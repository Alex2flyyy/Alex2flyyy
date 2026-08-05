"""Website audit orchestration.

Runs the evaluation tiers in increasing order of cost and stops as soon as the
answer is settled:

1. **Reachability + TLS** (~1 request). If the site is dead, nothing else
   matters and everything else would be wasted.
2. **Static HTML analysis** (0 extra requests). Most signals come from here.
3. **Rendered audit** (~1 browser page). Only when the static tier is
   inconclusive, or the site is JS-rendered, or screenshots are wanted.
4. **PageSpeed Insights** (~1 slow API call). Only for sites that will
   actually be pitched — it is the slowest step by an order of magnitude.
5. **Broken links / sitemap** (a handful of HEADs).

Tier gating is what makes 5,000 audits/night feasible. Running every tier on
every site would be roughly 30x the wall clock for signals that rarely change
the verdict.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from leadgen.compliance.robots import RobotsChecker
from leadgen.config import get_settings
from leadgen.domain import AuditOutcome, WebsiteAudit
from leadgen.evaluation.browser import BrowserAuditor, NullBrowserAuditor, RenderResult
from leadgen.evaluation.links import check_links, check_resource
from leadgen.evaluation.pagespeed import PageSpeedClient, PageSpeedResult
from leadgen.evaluation.parser import ParsedPage, parse_html
from leadgen.enrichment.normalize import normalize_url, root_host
from leadgen.http import HttpFetcher, probe_ssl
from leadgen.logging import get_logger
from leadgen.scoring.website_score import score_website

log = get_logger(__name__)


@dataclass(slots=True)
class AuditOptions:
    use_browser: bool = True
    use_pagespeed: bool = True
    check_broken_links: bool = True
    take_screenshots: bool = True
    check_contact_page: bool = True
    link_sample_size: int = 20


class WebsiteAuditor:
    def __init__(
        self,
        fetcher: HttpFetcher,
        *,
        browser: BrowserAuditor | NullBrowserAuditor | None = None,
        pagespeed: PageSpeedClient | None = None,
        robots: RobotsChecker | None = None,
        options: AuditOptions | None = None,
    ) -> None:
        self.settings = get_settings()
        self.fetcher = fetcher
        self.browser = browser or NullBrowserAuditor()
        self.pagespeed = pagespeed or PageSpeedClient(fetcher)
        self.robots = robots or RobotsChecker(fetcher.client)
        self.options = options or AuditOptions()
        self.audited = 0

    async def audit(self, url: str) -> WebsiteAudit:
        started = time.perf_counter()
        normalized = normalize_url(url)
        if not normalized:
            return _failed(url, AuditOutcome.ERROR, "unparseable URL", started)

        audit = WebsiteAudit(url=normalized)

        verdict = await self.robots.check(normalized)
        if not verdict.allowed:
            log.debug("audit.robots_blocked", url=normalized, reason=verdict.reason)
            return _failed(normalized, AuditOutcome.SKIPPED_ROBOTS, verdict.reason, started)

        # --- tier 1: reachability -------------------------------------
        response = await self.fetcher.get(normalized)
        audit.response_time_ms = response.elapsed_ms
        audit.redirect_chain = response.redirect_chain
        audit.final_url = response.final_url
        audit.http_status = response.status_code

        if response.error:
            outcome = {
                "dns_failure": AuditOutcome.DNS_FAILURE,
                "connection_refused": AuditOutcome.CONNECTION_REFUSED,
                "timeout": AuditOutcome.TIMEOUT,
                "tls_error": AuditOutcome.TLS_ERROR,
                "http_error": AuditOutcome.HTTP_ERROR,
            }.get(response.error_kind or "", AuditOutcome.ERROR)
            audit.outcome = outcome
            audit.error = response.error
            return self._finalize(audit, started)

        if response.status_code and response.status_code >= 400:
            audit.outcome = AuditOutcome.HTTP_ERROR
            audit.error = f"HTTP {response.status_code}"
            return self._finalize(audit, started)

        audit.uses_https = (audit.final_url or normalized).startswith("https://")
        audit.https_upgrade = normalized.startswith("http://") and audit.uses_https
        audit.security_headers = {
            key: response.headers.get(key, "")
            for key in (
                "strict-transport-security", "content-security-policy",
                "x-frame-options", "x-content-type-options",
            )
            if response.headers.get(key)
        }

        await self._check_tls(audit)

        # --- tier 2: static HTML --------------------------------------
        parsed = parse_html(response.text, audit.final_url or normalized)
        self._apply_parsed(audit, parsed)
        audit.page_weight_kb = response.content_length_kb

        if parsed.is_parked:
            audit.outcome = AuditOutcome.PARKED
            audit.problems.append("Domain appears parked or under construction")

        # --- tier 3: rendered audit -----------------------------------
        needs_render = self.options.use_browser and (
            parsed.is_minimal              # empty HTML = JS-rendered, must render
            or self.options.take_screenshots
            or not parsed.has_viewport_meta
            or bool({"React", "Vue", "Angular", "Wix", "Squarespace"} & set(parsed.tech_stack))
        )
        render: RenderResult | None = None
        if needs_render:
            render = await self.browser.render(
                audit.final_url or normalized, screenshots=self.options.take_screenshots
            )
            if render.ok:
                self._apply_render(audit, render, parsed)

        # --- tier 4 + 5: run the slow checks concurrently --------------
        tasks = []
        if self.options.use_pagespeed and self.pagespeed.available:
            tasks.append(("psi", self.pagespeed.analyze(audit.final_url or normalized)))
        if self.options.check_broken_links and parsed.internal_links:
            tasks.append(
                (
                    "links",
                    check_links(
                        self.fetcher,
                        parsed.internal_links + parsed.external_links,
                        sample_size=self.options.link_sample_size,
                    ),
                )
            )
        tasks.append(("sitemap", check_resource(self.fetcher, normalized, "/sitemap.xml")))
        tasks.append(("robots", check_resource(self.fetcher, normalized, "/robots.txt")))
        if self.options.check_contact_page and parsed.contact_page_url and not parsed.has_contact_form:
            tasks.append(("contact", self._probe_contact_page(parsed.contact_page_url)))

        results = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        for (name, _), outcome in zip(tasks, results, strict=True):
            if isinstance(outcome, BaseException):
                log.debug("audit.subtask_failed", url=normalized, task=name, error=str(outcome))
                continue
            self._apply_subtask(audit, name, outcome)

        return self._finalize(audit, started)

    # -- helpers ---------------------------------------------------------

    async def _check_tls(self, audit: WebsiteAudit) -> None:
        if not audit.uses_https:
            audit.ssl_valid = False
            return
        host = root_host(audit.final_url or audit.url)
        if not host:
            return
        tls = await probe_ssl(host)
        audit.ssl_valid = bool(tls["valid"])
        audit.ssl_expires_at = tls["expires_at"]
        audit.ssl_days_remaining = tls["days_remaining"]

    async def _probe_contact_page(self, url: str) -> bool:
        response = await self.fetcher.get(url, max_bytes=400_000)
        if not response.ok:
            return False
        return parse_html(response.text, url).has_contact_form

    def _apply_parsed(self, audit: WebsiteAudit, parsed: ParsedPage) -> None:
        audit.title = parsed.title
        audit.title_length = len(parsed.title or "")
        audit.meta_description = parsed.meta_description
        audit.meta_description_length = len(parsed.meta_description or "")
        audit.h1_count = parsed.h1_count
        audit.heading_outline_ok = parsed.heading_outline_ok
        audit.canonical_url = parsed.canonical_url
        audit.has_viewport_meta = parsed.has_viewport_meta
        audit.viewport_meta_content = parsed.viewport_content
        audit.has_favicon = parsed.has_favicon
        audit.has_open_graph = parsed.has_open_graph
        audit.has_structured_data = bool(parsed.structured_data_types)
        audit.lang_attribute = parsed.lang
        audit.word_count = parsed.word_count
        audit.copyright_year = parsed.copyright_year
        audit.tech_stack = parsed.tech_stack
        audit.uses_table_layout = parsed.uses_table_layout
        audit.is_flash_or_legacy = bool(
            {"flash", "frameset", "applet", "font_tag"} & set(parsed.legacy_markers)
        )
        audit.has_contact_form = parsed.has_contact_form
        audit.has_click_to_call = parsed.has_click_to_call
        audit.has_cta = parsed.has_cta
        audit.has_social_links = bool(parsed.social_links)
        audit.image_count = parsed.total_images
        audit.total_images = parsed.total_images
        audit.images_missing_alt = parsed.images_missing_alt
        audit.design_era = parsed.design_era
        audit.emails = parsed.emails
        audit.phones = parsed.phones
        audit.social_links = parsed.social_links

    def _apply_render(
        self, audit: WebsiteAudit, render: RenderResult, parsed: ParsedPage
    ) -> None:
        audit.horizontal_overflow = render.horizontal_overflow
        audit.smallest_font_px = render.smallest_font_px
        audit.tap_target_issues = render.tap_target_issues
        audit.mobile_friendly = render.mobile_friendly
        audit.screenshot_path = render.screenshot_path
        audit.screenshot_mobile_path = render.screenshot_mobile_path
        audit.request_count = render.request_count or audit.request_count
        if render.transfer_kb:
            audit.page_weight_kb = render.transfer_kb
        audit.mixed_content = render.mixed_content

        # Re-parse the rendered DOM when the raw HTML was effectively empty.
        # Without this, every Wix and React site scores as "no content".
        if parsed.is_minimal and render.rendered_html:
            rendered = parse_html(render.rendered_html, audit.final_url or audit.url)
            if rendered.word_count > parsed.word_count:
                self._apply_parsed(audit, rendered)
                audit.mobile_friendly = render.mobile_friendly
                audit.screenshot_path = render.screenshot_path
                audit.screenshot_mobile_path = render.screenshot_mobile_path

    def _apply_subtask(self, audit: WebsiteAudit, name: str, outcome: object) -> None:
        if name == "psi" and isinstance(outcome, PageSpeedResult) and outcome.ok:
            audit.psi_performance = outcome.performance
            audit.psi_seo = outcome.seo
            audit.psi_accessibility = outcome.accessibility
            audit.psi_best_practices = outcome.best_practices
            audit.lcp_ms = outcome.lcp_ms
            audit.fcp_ms = outcome.fcp_ms
            audit.cls = outcome.cls
            audit.tbt_ms = outcome.tbt_ms
            if outcome.total_bytes_kb:
                audit.page_weight_kb = outcome.total_bytes_kb
            if outcome.request_count:
                audit.request_count = outcome.request_count
            # Only trust PSI for mobile-friendliness when we didn't render
            # ourselves — our own overflow measurement is more direct.
            if audit.mobile_friendly is None:
                audit.mobile_friendly = outcome.mobile_friendly
        elif name == "links" and hasattr(outcome, "checked"):
            audit.broken_link_count = outcome.broken  # type: ignore[union-attr]
            audit.checked_link_count = outcome.checked  # type: ignore[union-attr]
        elif name == "sitemap":
            audit.has_sitemap = bool(outcome)
        elif name == "robots":
            audit.has_robots_txt = bool(outcome)
        elif name == "contact":
            if bool(outcome):
                audit.has_contact_form = True

    def _finalize(self, audit: WebsiteAudit, started: float) -> WebsiteAudit:
        audit.duration_ms = int((time.perf_counter() - started) * 1000)
        audit.checked_at = datetime.utcnow()
        score_website(audit)
        self.audited += 1
        log.debug(
            "audit.complete",
            url=audit.url,
            outcome=audit.outcome.value,
            score=round(audit.score, 1),
            status=audit.status.value,
            ms=audit.duration_ms,
        )
        return audit


def _failed(url: str, outcome: AuditOutcome, error: str, started: float) -> WebsiteAudit:
    audit = WebsiteAudit(url=url, outcome=outcome, error=error)
    audit.duration_ms = int((time.perf_counter() - started) * 1000)
    score_website(audit)
    return audit


def domain_of(url: str) -> str | None:
    try:
        return urlparse(url).netloc.lower() or None
    except ValueError:
        return None
