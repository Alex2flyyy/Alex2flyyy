"""Google PageSpeed Insights integration.

Runs a real Lighthouse audit on Google's infrastructure and returns
Performance / SEO / Accessibility / Best-Practices scores plus Core Web Vitals.

Why this rather than measuring load time ourselves: a number *from Google* is
persuasive to a business owner in a way that "our tool says your site is slow"
never is. "Google scores your site 23 out of 100 on mobile" ends the argument.
It is also the same metric that feeds Google's own ranking signals, so it is
directly tied to the money.

Costs: the API is free with a key at 25,000 requests/day, but each call takes
10-30 seconds because a real browser runs. That latency is why this stage is
gated to sites that already look promising rather than run on everything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from leadgen.config import get_settings
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


@dataclass(slots=True)
class PageSpeedResult:
    ok: bool = False
    error: str | None = None
    strategy: str = "mobile"

    performance: float | None = None
    seo: float | None = None
    accessibility: float | None = None
    best_practices: float | None = None

    lcp_ms: float | None = None
    fcp_ms: float | None = None
    cls: float | None = None
    tbt_ms: float | None = None
    speed_index_ms: float | None = None
    interactive_ms: float | None = None

    total_bytes_kb: float | None = None
    request_count: int | None = None
    unused_css_kb: float | None = None
    unoptimized_images_kb: float | None = None

    viewport_ok: bool | None = None
    font_size_ok: bool | None = None
    tap_targets_ok: bool | None = None
    has_https: bool | None = None

    failed_audits: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failed_audits is None:
            self.failed_audits = []

    @property
    def is_slow(self) -> bool:
        """Google's own "poor" threshold for Largest Contentful Paint is 4s."""
        if self.lcp_ms is not None:
            return self.lcp_ms > 4000
        return self.performance is not None and self.performance < 50

    @property
    def mobile_friendly(self) -> bool | None:
        checks = [self.viewport_ok, self.font_size_ok, self.tap_targets_ok]
        known = [c for c in checks if c is not None]
        if not known:
            return None
        return all(known)


class PageSpeedClient:
    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher
        self.settings = get_settings()
        self.api_key = self.settings.pagespeed_key
        self.api_calls = 0
        self.errors = 0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def analyze(self, url: str, strategy: str = "mobile") -> PageSpeedResult:
        """Run Lighthouse against ``url``.

        Mobile strategy by default: it is the stricter test, it reflects where
        local-business traffic actually comes from, and it is what Google uses
        for indexing.
        """
        if not self.available:
            return PageSpeedResult(ok=False, error="no PageSpeed API key configured")

        params = {
            "url": url,
            "strategy": strategy,
            "key": self.api_key,
            "category": ["PERFORMANCE", "SEO", "ACCESSIBILITY", "BEST_PRACTICES"],
        }
        self.api_calls += 1
        # Lighthouse genuinely takes this long; a short timeout just wastes the
        # call that was already spent server-side.
        response = await self.fetcher.get(PSI_URL, params=params)

        if not response.ok:
            self.errors += 1
            detail = response.error or f"HTTP {response.status_code}"
            log.debug("psi.failed", url=url, error=detail)
            return PageSpeedResult(ok=False, error=detail[:200], strategy=strategy)

        try:
            data = json.loads(response.text)
        except ValueError:
            self.errors += 1
            return PageSpeedResult(ok=False, error="invalid JSON from PageSpeed", strategy=strategy)

        if "error" in data:
            message = data["error"].get("message", "unknown PageSpeed error")
            return PageSpeedResult(ok=False, error=message[:200], strategy=strategy)

        return _parse(data, strategy)


def _parse(data: dict[str, Any], strategy: str) -> PageSpeedResult:
    lighthouse = data.get("lighthouseResult") or {}
    categories = lighthouse.get("categories") or {}
    audits = lighthouse.get("audits") or {}

    def category(name: str) -> float | None:
        raw = (categories.get(name) or {}).get("score")
        return round(raw * 100, 1) if isinstance(raw, int | float) else None

    def numeric(audit_id: str) -> float | None:
        value = (audits.get(audit_id) or {}).get("numericValue")
        return round(float(value), 1) if isinstance(value, int | float) else None

    def passed(audit_id: str) -> bool | None:
        audit = audits.get(audit_id)
        if not audit:
            return None
        score = audit.get("score")
        if score is None:
            return None
        return score >= 0.9

    result = PageSpeedResult(
        ok=True,
        strategy=strategy,
        performance=category("performance"),
        seo=category("seo"),
        accessibility=category("accessibility"),
        best_practices=category("best-practices"),
        lcp_ms=numeric("largest-contentful-paint"),
        fcp_ms=numeric("first-contentful-paint"),
        tbt_ms=numeric("total-blocking-time"),
        speed_index_ms=numeric("speed-index"),
        interactive_ms=numeric("interactive"),
        viewport_ok=passed("viewport"),
        font_size_ok=passed("font-size"),
        tap_targets_ok=passed("tap-targets"),
        has_https=passed("is-on-https"),
    )

    # CLS is unitless, so it must not go through the ms rounding above.
    cls_value = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
    if isinstance(cls_value, int | float):
        result.cls = round(float(cls_value), 3)

    if total_bytes := numeric("total-byte-weight"):
        result.total_bytes_kb = round(total_bytes / 1024, 1)
    if unused_css := numeric("unused-css-rules"):
        result.unused_css_kb = round(unused_css / 1024, 1)
    if images := numeric("uses-optimized-images"):
        result.unoptimized_images_kb = round(images / 1024, 1)

    if items := ((audits.get("network-requests") or {}).get("details") or {}).get("items"):
        result.request_count = len(items)

    # Collect the human-readable titles of failed audits. These become bullet
    # points in the prospect report, so they need to read as findings, not IDs.
    interesting = {
        "viewport", "font-size", "tap-targets", "is-on-https", "uses-https",
        "meta-description", "document-title", "image-alt", "link-text",
        "crawlable-anchors", "hreflang", "render-blocking-resources",
        "uses-responsive-images", "unminified-css", "unminified-javascript",
        "uses-text-compression", "server-response-time", "redirects",
        "errors-in-console", "color-contrast", "html-has-lang",
    }
    for audit_id in interesting:
        audit = audits.get(audit_id)
        if not audit:
            continue
        score = audit.get("score")
        if score is not None and score < 0.9:
            title = audit.get("title") or audit_id
            result.failed_audits.append(title)

    return result
