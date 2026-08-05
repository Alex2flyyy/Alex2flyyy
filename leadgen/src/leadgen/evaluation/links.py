"""Broken-link checking.

Deliberately shallow: a sample of links from the homepage, checked with HEAD
requests, concurrency-capped. The goal is a defensible claim — "6 of the 20
links we checked are dead" — not an exhaustive site crawl. Exhaustive crawling
of a prospect's site is slow, rude, and unnecessary to make the point.

HEAD is tried first and falls back to a ranged GET, because a meaningful
minority of servers reject HEAD with 405 while serving GET perfectly well.
Counting those as broken would be a false accusation in a sales email.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlparse

from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

# 401/403 mean "exists but gated" — a login wall is not a broken link.
NOT_BROKEN_STATUSES = {401, 403, 405, 429, 999}


@dataclass(slots=True)
class LinkCheckResult:
    checked: int = 0
    broken: int = 0
    broken_urls: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def broken_ratio(self) -> float:
        return self.broken / self.checked if self.checked else 0.0


async def check_links(
    fetcher: HttpFetcher,
    links: list[str],
    *,
    sample_size: int = 20,
    concurrency: int = 5,
) -> LinkCheckResult:
    result = LinkCheckResult()
    if not links:
        return result

    # Sample across the list rather than taking the first N: the first 20
    # links on any site are the nav menu, which is the least likely part to be
    # broken and the least informative.
    if len(links) > sample_size:
        step = max(1, len(links) // sample_size)
        sample = links[::step][:sample_size]
    else:
        sample = links

    semaphore = asyncio.Semaphore(concurrency)

    async def check(url: str) -> tuple[str, bool]:
        async with semaphore:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return url, False

            head = await fetcher.head(url)
            if head.status_code and head.status_code in NOT_BROKEN_STATUSES:
                # Ambiguous. Confirm with a real GET before calling it broken.
                confirm = await fetcher.get(url, max_bytes=2048)
                if confirm.status_code and confirm.status_code < 400:
                    return url, False
                return url, bool(confirm.status_code and confirm.status_code >= 400)
            if head.error:
                # DNS failures and refused connections are genuinely broken;
                # timeouts on a slow host are not conclusive.
                return url, head.error_kind in {"dns_failure", "connection_refused"}
            return url, bool(head.status_code and head.status_code >= 400)

    outcomes = await asyncio.gather(
        *(check(url) for url in sample), return_exceptions=True
    )

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            result.skipped += 1
            continue
        url, is_broken = outcome
        result.checked += 1
        if is_broken:
            result.broken += 1
            if len(result.broken_urls) < 10:
                result.broken_urls.append(url)

    return result


async def check_resource(fetcher: HttpFetcher, base_url: str, path: str) -> bool:
    """Whether a well-known path exists (``/sitemap.xml``, ``/robots.txt``)."""
    parsed = urlparse(base_url)
    url = f"{parsed.scheme}://{parsed.netloc}{path}"
    response = await fetcher.get(url, max_bytes=4096)
    if not response.ok:
        return False
    # Single-page-app hosts serve index.html for every unknown path, so a 200
    # alone proves nothing — check the payload actually looks like the resource.
    body = response.text[:400].lower()
    if path.endswith(".xml"):
        return "<?xml" in body or "<urlset" in body or "<sitemapindex" in body
    if path.endswith("robots.txt"):
        return "user-agent" in body or "disallow" in body or "sitemap" in body
    return True
