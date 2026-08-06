"""Compliance policy helpers.

This system collects publicly listed business contact information for B2B
outreach. That is lawful in the United States, but it is not unconditional.
The rules encoded here are the ones that actually bite:

* **CAN-SPAM** — commercial email must identify itself, carry a valid physical
  postal address, and honor opt-outs within 10 business days. Suppression is
  enforced at export time by :func:`filter_suppressed`.
* **TCPA** — automated calls and texts to *mobile* numbers need consent, and
  the penalties are per-message. This system never dials; it produces a list a
  human works. Do not wire it to an autodialer without counsel.
* **National / state Do-Not-Call registries** — apply to consumers. Business
  lines are generally exempt, but sole proprietors often list a cell.
  Scrub against the DNC registry before any calling campaign.
* **CCPA/CPRA** — California residents may request deletion. Sole proprietors
  count as individuals here. :func:`build_deletion_record` produces what you
  need to service such a request.
* **Provider terms** — Google Places data carries caching and attribution
  requirements. See ``docs/COMPLIANCE.md``.

None of this is legal advice. It is the shape of the obligations; get counsel
before running outreach at volume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# Google's Places terms permit caching content only to improve performance,
# and place IDs indefinitely. 30 days is the widely used conservative refresh
# interval for cached place *content*.
PLACES_CONTENT_MAX_CACHE_DAYS = 30


@dataclass(slots=True)
class ComplianceFlags:
    can_email: bool = True
    can_call: bool = True
    can_text: bool = False  # texting needs prior express consent. Default off.
    notes: list[str] | None = None


def is_stale_provider_content(fetched_at: datetime) -> bool:
    """True when cached provider content is past its permitted retention."""
    return datetime.utcnow() - fetched_at > timedelta(days=PLACES_CONTENT_MAX_CACHE_DAYS)


def filter_suppressed(rows: list[dict[str, Any]], suppressions: dict[str, set[str]]) -> list[dict]:
    """Drop anything on the do-not-contact list.

    Called by every exporter and by the report generator. A suppressed record
    must never reach a channel where someone could act on it, which is why the
    check lives at the boundary rather than in the scorer.
    """
    if not suppressions:
        return rows

    emails = suppressions.get("email", set())
    phones = suppressions.get("phone", set())
    domains = suppressions.get("domain", set())
    names = suppressions.get("business", set())

    kept = []
    for row in rows:
        email = (row.get("email") or "").lower()
        phone = (row.get("phone_e164") or row.get("phone") or "").lower()
        domain = (row.get("website_domain") or "").lower()
        name = (row.get("business_name") or row.get("name") or "").lower()
        if email and email in emails:
            continue
        if phone and phone in phones:
            continue
        if domain and domain in domains:
            continue
        if name and name in names:
            continue
        kept.append(row)
    return kept


def build_deletion_record(business: Any) -> dict[str, Any]:
    """What to hand back when someone exercises a deletion right."""
    return {
        "business_id": business.id,
        "name": business.name,
        "address": business.street_address,
        "phone": business.phone,
        "email": business.email,
        "sources": [business.source],
        "first_collected": business.first_discovered_at.isoformat()
        if business.first_discovered_at
        else None,
        "purpose": "B2B marketing prospect research for website design services",
        "public_source": "publicly listed business directory information",
    }


def outreach_disclaimer(company_name: str, postal_address: str) -> str:
    """CAN-SPAM footer. Every commercial email must carry one."""
    return (
        f"This message was sent by {company_name}, {postal_address}. "
        "You are receiving it because your business is publicly listed and we "
        "believe our website services may be relevant. "
        "Reply STOP or click unsubscribe and we will remove you immediately."
    )
