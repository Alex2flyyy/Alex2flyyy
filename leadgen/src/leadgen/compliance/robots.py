"""robots.txt handling.

Auditing a prospect's homepage is a single polite GET of a public page — the
same thing a human evaluating the business would do. We still honor robots.txt
by default because:

* It is the operator's stated preference, and ignoring it is what gets an IP
  block that kills the whole pipeline.
* Some sites explicitly disallow ``/`` for non-search-engine agents, and
  crawling those anyway creates a real legal and reputational exposure.

Set ``LEADGEN_RESPECT_ROBOTS_TXT=false`` only if you have a specific, defensible
reason. Note that robots.txt governs *automated fetching*, not what you may do
with facts you observe.
"""

from __future__ import annotations

import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from leadgen.config import get_settings
from leadgen.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class RobotsVerdict:
    allowed: bool
    reason: str
    crawl_delay: float | None = None


class RobotsChecker:
    """Fetches and caches robots.txt per host for the life of a run.

    A missing, malformed, or erroring robots.txt is treated as "allowed" — that
    matches the convention every major crawler follows. A 401/403 on
    robots.txt itself is treated as a *disallow*, since the operator is
    signaling that automated clients are unwelcome.
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str | None = None) -> None:
        self._client = client
        self._settings = get_settings()
        self._user_agent = user_agent or self._settings.user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._denied: dict[str, str] = {}

    async def _parser(self, host: str, scheme: str) -> urllib.robotparser.RobotFileParser | None:
        if host in self._cache:
            return self._cache[host]

        robots_url = f"{scheme}://{host}/robots.txt"
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            response = await self._client.get(
                robots_url, timeout=8.0, follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            )
            if response.status_code in (401, 403):
                self._denied[host] = f"robots.txt returned {response.status_code}"
            elif response.status_code < 400 and response.text:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
        except (httpx.HTTPError, UnicodeDecodeError) as exc:
            log.debug("robots.fetch_failed", host=host, error=str(exc))

        self._cache[host] = parser
        return parser

    async def check(self, url: str) -> RobotsVerdict:
        if not self._settings.respect_robots_txt:
            return RobotsVerdict(True, "robots checking disabled")

        try:
            parsed = urlparse(url)
        except ValueError:
            return RobotsVerdict(False, "unparseable url")
        host = parsed.netloc
        if not host:
            return RobotsVerdict(False, "no host in url")

        parser = await self._parser(host, parsed.scheme or "https")

        if host in self._denied:
            return RobotsVerdict(False, self._denied[host])
        if parser is None:
            return RobotsVerdict(True, "no robots.txt")

        allowed = parser.can_fetch(self._user_agent, url)
        delay = parser.crawl_delay(self._user_agent)
        return RobotsVerdict(
            allowed,
            "allowed by robots.txt" if allowed else "disallowed by robots.txt",
            float(delay) if delay else None,
        )

    async def has_robots_txt(self, url: str) -> bool:
        parsed = urlparse(url)
        parser = await self._parser(parsed.netloc, parsed.scheme or "https")
        return parser is not None
