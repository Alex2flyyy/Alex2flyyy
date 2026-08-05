"""Deduplication.

Three tiers, cheapest first:

1. **Exact source identity** — same provider, same id. Free, and catches
   re-running the same search.
2. **Identity hash** — normalized name + street + ZIP. Catches the same
   business arriving from a different provider.
3. **Fuzzy match** — phone or domain plus name similarity. Catches suite
   changes, punctuation drift, and "Inc" appearing and disappearing.

Tier 3 runs against a small candidate set pulled by an indexed column, never
against the whole table, so cost stays flat as the database grows.
"""

from __future__ import annotations

from dataclasses import dataclass

from leadgen.domain import RawBusiness
from leadgen.enrichment.normalize import (
    compute_dedupe_key,
    display_name_tokens,
    extract_domain,
    normalize_name,
    normalize_phone,
    normalize_street,
    normalize_zip,
)
from leadgen.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class MatchDecision:
    is_duplicate: bool
    existing_id: int | None = None
    confidence: float = 0.0
    rule: str = ""


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def name_similarity(name_a: str | None, name_b: str | None) -> float:
    """Blend of token overlap and exact normalized equality.

    Token overlap alone rates "Bob's Auto Repair" and "Bob's Auto Body" too
    highly; exact normalized equality alone misses "Bob's Auto" vs "Bobs Auto
    Repair". Taking the max of both with the exact match weighted to 1.0 gives
    the behavior you want at both ends.
    """
    if not name_a or not name_b:
        return 0.0
    if normalize_name(name_a) == normalize_name(name_b):
        return 1.0
    return jaccard(display_name_tokens(name_a), display_name_tokens(name_b))


def build_identity(raw: RawBusiness) -> dict[str, str | None]:
    """Everything the persistence layer needs to store and dedupe a record."""
    phone_e164 = normalize_phone(raw.phone)
    domain = extract_domain(raw.website)
    return {
        "source_key": f"{raw.source}:{raw.source_id}",
        "dedupe_key": compute_dedupe_key(
            raw.name, raw.street_address, raw.zip_code, phone_e164=phone_e164
        ),
        "normalized_name": normalize_name(raw.name),
        "phone_e164": phone_e164,
        "website_domain": domain,
        "normalized_street": normalize_street(raw.street_address),
        "normalized_zip": normalize_zip(raw.zip_code),
    }


class InMemoryDeduper:
    """Within-run deduplication.

    A single pipeline run queries overlapping search cells on purpose (grids
    overlap so nothing falls between them), so the same restaurant comes back
    five times. Filtering in memory before touching the database saves five
    round trips per business, which at 5,000 leads/day is the difference
    between a two-minute and a twenty-minute stage.
    """

    def __init__(self) -> None:
        self._source_keys: set[str] = set()
        self._dedupe_keys: set[str] = set()
        self._phones: dict[str, str] = {}   # phone -> normalized name
        self._domains: dict[str, str] = {}  # domain -> normalized name
        self.duplicates_dropped = 0

    def seen(self, raw: RawBusiness) -> bool:
        ident = build_identity(raw)
        source_key = ident["source_key"] or ""
        dedupe_key = ident["dedupe_key"] or ""
        norm_name = ident["normalized_name"] or ""

        if source_key in self._source_keys or dedupe_key in self._dedupe_keys:
            self.duplicates_dropped += 1
            return True

        phone = ident["phone_e164"]
        if phone and phone in self._phones:
            # Shared phone + similar name = same business. Shared phone alone is
            # not enough: answering services and multi-location owners reuse a
            # single number across genuinely distinct listings.
            if name_similarity(self._phones[phone], raw.name) >= 0.5:
                self.duplicates_dropped += 1
                return True

        domain = ident["website_domain"]
        if domain and domain in self._domains:
            if name_similarity(self._domains[domain], raw.name) >= 0.6:
                self.duplicates_dropped += 1
                return True

        self._source_keys.add(source_key)
        self._dedupe_keys.add(dedupe_key)
        if phone:
            self._phones.setdefault(phone, raw.name)
        if domain:
            self._domains.setdefault(domain, raw.name)
        return False

    def filter(self, businesses: list[RawBusiness]) -> list[RawBusiness]:
        return [b for b in businesses if not self.seen(b)]


async def find_existing(session, raw: RawBusiness) -> MatchDecision:  # type: ignore[no-untyped-def]
    """Cross-run deduplication against the database."""
    from leadgen.db.repositories import BusinessRepository

    repo = BusinessRepository(session)
    ident = build_identity(raw)

    existing = await repo.get_by_source_key(ident["source_key"] or "")
    if existing:
        return MatchDecision(True, existing.id, 1.0, "source_key")

    existing = await repo.get_by_dedupe_key(ident["dedupe_key"] or "")
    if existing:
        return MatchDecision(True, existing.id, 0.98, "dedupe_key")

    candidate = await repo.find_probable_duplicate(
        phone_e164=ident["phone_e164"],
        website_domain=ident["website_domain"],
        normalized_name=ident["normalized_name"] or "",
    )
    if candidate:
        similarity = name_similarity(candidate.name, raw.name)
        if similarity >= 0.5:
            return MatchDecision(True, candidate.id, similarity, "fuzzy_phone_or_domain")

    return MatchDecision(False)
