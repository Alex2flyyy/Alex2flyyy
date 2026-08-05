"""Contact discovery.

Finds publicly published ways to reach a business: email addresses, direct
phone numbers, social profiles, and whether a contact form exists.

Scope discipline matters here, both for cost and for legality. This module
only reads pages the business publishes on its own domain — the same pages a
customer would read. It does not query email-finding services, does not guess
addresses by pattern (``firstname@domain``), and does not touch personal social
accounts. Publicly listed business contact details for B2B outreach is the
defensible position; inferred personal addresses is not.

At most ``LEADGEN_CRAWL_MAX_PAGES_PER_SITE`` pages are fetched per business,
prioritized by how likely they are to carry contact details.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from leadgen.compliance.robots import RobotsChecker
from leadgen.config import get_settings
from leadgen.enrichment.normalize import (
    extract_domain,
    guess_owner_name,
    is_generic_email,
    normalize_phone,
)
from leadgen.evaluation.parser import parse_html
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

# Ordered by how often they actually carry an address.
CONTACT_PATH_PRIORITY = [
    "/contact",
    "/contact-us",
    "/contactus",
    "/about",
    "/about-us",
    "/team",
    "/staff",
    "/our-team",
    "/get-a-quote",
    "/quote",
    "/estimate",
]

_CONTACT_LINK_RE = re.compile(r"contact|about|team|staff|quote|estimate", re.I)

# Owner/decision-maker titles worth capturing when they appear near a name.
_OWNER_TITLE_RE = re.compile(
    r"\b(owner|founder|president|principal|proprietor|ceo|managing partner|"
    r"office manager|general manager)\b",
    re.I,
)


@dataclass(slots=True)
class ContactFindings:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: dict[str, str] = field(default_factory=dict)
    has_contact_form: bool = False
    contact_form_url: str | None = None
    owner_name: str | None = None
    pages_fetched: int = 0

    @property
    def primary_email(self) -> str | None:
        if not self.emails:
            return None
        # A named person outranks info@ — you want the decision maker.
        personal = [e for e in self.emails if not is_generic_email(e)]
        return personal[0] if personal else self.emails[0]

    def as_contact_rows(self) -> list[dict[str, object]]:
        """Shape expected by ``ContactRepository.upsert_many``."""
        rows: list[dict[str, object]] = []
        for index, email in enumerate(self.emails):
            generic = is_generic_email(email)
            rows.append(
                {
                    "kind": "email",
                    "value": email,
                    "source": "website",
                    "is_generic": generic,
                    "is_primary": email == self.primary_email,
                    "confidence": 0.6 if generic else 0.85,
                    "person_name": None if generic else guess_owner_name(email),
                    "label": "generic inbox" if generic else "direct",
                }
            )
            if index >= 4:
                break
        for phone in self.phones[:3]:
            rows.append(
                {
                    "kind": "phone",
                    "value": phone,
                    "source": "website",
                    "confidence": 0.8,
                    "is_primary": phone == self.phones[0],
                }
            )
        for platform, url in self.social_links.items():
            rows.append(
                {
                    "kind": "social",
                    "value": url,
                    "label": platform,
                    "source": "website",
                    "confidence": 0.9,
                }
            )
        if self.contact_form_url:
            rows.append(
                {
                    "kind": "form",
                    "value": self.contact_form_url,
                    "source": "website",
                    "confidence": 0.9,
                    "label": "contact form",
                }
            )
        return rows


class ContactEnricher:
    def __init__(self, fetcher: HttpFetcher, robots: RobotsChecker | None = None) -> None:
        self.fetcher = fetcher
        self.settings = get_settings()
        self.robots = robots or RobotsChecker(fetcher.client)
        self.sites_crawled = 0

    async def enrich(self, website_url: str, *, seed_html: str | None = None) -> ContactFindings:
        findings = ContactFindings()
        domain = extract_domain(website_url)
        if not domain:
            return findings

        max_pages = max(1, self.settings.crawl_max_pages_per_site)
        visited: set[str] = set()

        pages_to_visit = [website_url]
        if seed_html:
            self._absorb(findings, seed_html, website_url)
            pages_to_visit = self._candidate_pages(seed_html, website_url)[: max_pages - 1]
            visited.add(_canonical(website_url))
            findings.pages_fetched = 1

        for url in pages_to_visit:
            if findings.pages_fetched >= max_pages:
                break
            key = _canonical(url)
            if key in visited:
                continue
            visited.add(key)

            # Stay on the business's own domain. Following an outbound link
            # would mean crawling a third party we have no relationship with.
            if extract_domain(url) != domain:
                continue

            verdict = await self.robots.check(url)
            if not verdict.allowed:
                continue

            response = await self.fetcher.get(url, max_bytes=600_000)
            findings.pages_fetched += 1
            if not response.ok or not response.text:
                continue

            self._absorb(findings, response.text, url)

            # Stop early once we have a direct (non-generic) address; more
            # pages will not improve on that.
            if findings.primary_email and not is_generic_email(findings.primary_email):
                break

            if len(pages_to_visit) < max_pages and not seed_html:
                pages_to_visit.extend(
                    p
                    for p in self._candidate_pages(response.text, url)
                    if _canonical(p) not in visited
                )

        self.sites_crawled += 1
        return findings

    def _absorb(self, findings: ContactFindings, html: str, url: str) -> None:
        page = parse_html(html, url)

        for email in page.emails:
            if email not in findings.emails and len(findings.emails) < 8:
                findings.emails.append(email)

        for phone in page.phones:
            normalized = normalize_phone(phone) or phone
            if normalized not in findings.phones and len(findings.phones) < 5:
                findings.phones.append(normalized)

        for platform, link in page.social_links.items():
            findings.social_links.setdefault(platform, link)

        if page.has_contact_form:
            findings.has_contact_form = True
            findings.contact_form_url = findings.contact_form_url or url

        if findings.owner_name is None:
            findings.owner_name = self._find_owner(page.text_sample)

    def _find_owner(self, text: str) -> str | None:
        """Look for "Name, Owner" or "Owner: Name" near a title keyword.

        Conservative on purpose: a wrong owner name in a cold email is worse
        than no name at all.
        """
        for match in _OWNER_TITLE_RE.finditer(text[:8000]):
            window = text[max(0, match.start() - 80) : match.end() + 80]
            name_match = re.search(r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-z]{1,20})\b", window)
            if name_match:
                candidate = f"{name_match.group(1)} {name_match.group(2)}"
                # Filter out headings that merely look like names.
                if candidate.lower() not in {"contact us", "about us", "our team", "meet the"}:
                    return candidate
        return None

    def _candidate_pages(self, html: str, base_url: str) -> list[str]:
        """Rank same-site links by how likely they carry contact details."""
        page = parse_html(html, base_url)
        scored: list[tuple[int, str]] = []

        for link in page.internal_links:
            path = urlparse(link).path.lower().rstrip("/")
            if not path or path == "/":
                continue
            score = 0
            for index, known in enumerate(CONTACT_PATH_PRIORITY):
                if path.endswith(known) or path == known:
                    score = 100 - index
                    break
            if not score and _CONTACT_LINK_RE.search(path):
                score = 30
            if score:
                scored.append((score, link))

        scored.sort(key=lambda pair: -pair[0])

        out: list[str] = []
        seen: set[str] = set()
        for _score, link in scored:
            key = _canonical(link)
            if key not in seen:
                seen.add(key)
                out.append(link)

        # Always try /contact even if it isn't linked from the homepage.
        if not out:
            out = [urljoin(base_url, path) for path in CONTACT_PATH_PRIORITY[:2]]
        return out


def _canonical(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"
