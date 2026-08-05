"""Headless-browser rendering audit.

Static HTML analysis misses everything a JavaScript-rendered site does after
load — and a growing share of small-business sites are Wix/Squarespace/React
builds where the raw HTML is nearly empty. Without rendering, those sites look
like "no content" and score wrongly.

This module answers three questions that genuinely require a real browser:

1. **Does it actually overflow on a phone?** Comparing
   ``scrollWidth`` to ``clientWidth`` at 390px is the definitive
   mobile-responsiveness test. A viewport meta tag is a claim; horizontal
   overflow is proof.
2. **Is the text readable?** Computed font sizes, not CSS source.
3. **What does it look like?** Screenshots, which are the single most
   persuasive artifact in the outreach email.

One browser instance is shared across the whole run and each site gets its own
isolated context. Launching Chromium per site would add ~1s of pure overhead
per audit; sharing a context between sites would leak cookies and storage
across prospects.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leadgen.config import get_settings
from leadgen.logging import get_logger

log = get_logger(__name__)

MOBILE_VIEWPORT = {"width": 390, "height": 844}   # iPhone 14 class
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}

# Blocking these cuts render time roughly in half without changing layout.
BLOCKED_RESOURCE_TYPES = {"media", "font"}
BLOCKED_URL_PATTERNS = re.compile(
    r"(doubleclick\.net|googlesyndication|googletagservices|facebook\.com/tr|"
    r"hotjar\.com|fullstory\.com|mouseflow|criteo|taboola|outbrain)",
    re.I,
)


@dataclass(slots=True)
class RenderResult:
    ok: bool = False
    error: str | None = None

    rendered_html: str = ""
    final_url: str | None = None
    load_time_ms: int | None = None
    dom_content_loaded_ms: int | None = None

    horizontal_overflow: bool = False
    scroll_width: int | None = None
    client_width: int | None = None
    overflow_px: int = 0
    smallest_font_px: float | None = None
    tap_target_issues: int = 0
    tiny_text_ratio: float = 0.0

    request_count: int = 0
    transfer_kb: float = 0.0
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    mixed_content: bool = False

    color_count: int = 0
    font_families: list[str] = field(default_factory=list)
    has_hero_image: bool = False
    above_fold_text_chars: int = 0

    screenshot_path: str | None = None
    screenshot_mobile_path: str | None = None

    @property
    def mobile_friendly(self) -> bool:
        return not self.horizontal_overflow and (
            self.smallest_font_px is None or self.smallest_font_px >= 11
        )


# JavaScript evaluated in-page. Kept as one call to avoid a round trip per
# metric — each `evaluate` is IPC to the browser process.
_LAYOUT_PROBE = """
() => {
  const doc = document.documentElement;
  const body = document.body || doc;

  // Find the element actually causing overflow, not just that it exists.
  let widest = 0;
  const els = Array.from(document.querySelectorAll('body *')).slice(0, 3000);
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > widest) widest = r.right;
  }

  const sizes = [];
  const families = new Set();
  const colors = new Set();
  let tinyCount = 0;
  let textNodes = 0;
  const textEls = Array.from(document.querySelectorAll(
    'p,span,li,td,a,div,h1,h2,h3,h4,h5,h6,label'
  )).slice(0, 1200);

  for (const el of textEls) {
    const text = (el.textContent || '').trim();
    if (!text || text.length < 2) continue;
    if (el.children.length > 0 && text.length > 200) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const size = parseFloat(cs.fontSize);
    if (size > 0) {
      sizes.push(size);
      textNodes++;
      if (size < 12) tinyCount++;
    }
    if (cs.fontFamily) families.add(cs.fontFamily.split(',')[0].replace(/["']/g, '').trim());
    if (cs.color) colors.add(cs.color);
    if (cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)') {
      colors.add(cs.backgroundColor);
    }
  }

  // Tap targets: interactive elements smaller than ~44px, the accessibility
  // guideline minimum, and the reason people mis-tap on phones.
  let tapIssues = 0;
  const interactive = Array.from(
    document.querySelectorAll('a,button,input,select,textarea,[role=button]')
  ).slice(0, 500);
  for (const el of interactive) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.width < 44 || r.height < 44) tapIssues++;
  }

  const foldText = Array.from(document.querySelectorAll('body *'))
    .filter(el => { const r = el.getBoundingClientRect(); return r.top < window.innerHeight && r.top >= 0; })
    .slice(0, 200)
    .map(el => (el.childElementCount === 0 ? (el.textContent || '') : ''))
    .join(' ').trim();

  const heroCandidates = Array.from(document.querySelectorAll('img,[style*="background-image"]'))
    .filter(el => { const r = el.getBoundingClientRect(); return r.top < 700 && r.width > 250 && r.height > 140; });

  return {
    scrollWidth: Math.max(doc.scrollWidth, body.scrollWidth, Math.ceil(widest)),
    clientWidth: doc.clientWidth,
    smallestFont: sizes.length ? Math.min(...sizes) : null,
    tinyRatio: textNodes ? tinyCount / textNodes : 0,
    tapIssues,
    fontFamilies: Array.from(families).slice(0, 10),
    colorCount: colors.size,
    aboveFoldTextChars: foldText.length,
    hasHeroImage: heroCandidates.length > 0,
    html: document.documentElement.outerHTML
  };
}
"""


class BrowserAuditor:
    """Shared Playwright browser. Use as an async context manager.

    Import of ``playwright`` is deferred to construction time so the rest of
    the system — API, exports, scoring — runs on a machine that never
    installed a browser.
    """

    def __init__(self, *, headless: bool = True, concurrency: int | None = None) -> None:
        self.settings = get_settings()
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._semaphore = asyncio.Semaphore(
            concurrency or max(2, self.settings.max_concurrent_audits // 2)
        )
        self.screenshot_dir = Path(self.settings.screenshot_dir)
        self.renders = 0
        self.failures = 0

    async def __aenter__(self) -> BrowserAuditor:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-dev-shm-usage",   # /dev/shm is tiny in containers
                "--disable-gpu",
                "--no-sandbox",              # required as non-root in Docker
                "--disable-background-timer-throttling",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        log.info("browser.started", headless=self.headless)

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._browser:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._playwright:
                await self._playwright.stop()
        self._browser = None
        self._playwright = None
        log.info("browser.stopped", renders=self.renders, failures=self.failures)

    async def render(
        self, url: str, *, screenshots: bool | None = None, timeout_ms: int | None = None
    ) -> RenderResult:
        if self._browser is None:
            return RenderResult(ok=False, error="browser not started")

        take_screenshots = (
            self.settings.enable_screenshots if screenshots is None else screenshots
        )
        timeout = timeout_ms or self.settings.audit_timeout_seconds * 1000

        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self._render(url, take_screenshots, timeout),
                    timeout=(timeout / 1000) + 15,
                )
            except asyncio.TimeoutError:
                self.failures += 1
                return RenderResult(ok=False, error="render timed out")
            except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
                self.failures += 1
                log.debug("browser.render_failed", url=url, error=str(exc))
                return RenderResult(ok=False, error=str(exc)[:300])

    async def _render(self, url: str, screenshots: bool, timeout: int) -> RenderResult:
        result = RenderResult()
        context = await self._browser.new_context(
            viewport=MOBILE_VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            ignore_https_errors=True,  # we want to *observe* cert problems, not abort on them
            java_script_enabled=True,
        )

        transfer_bytes = 0
        request_count = 0
        console_errors: list[str] = []
        failed: list[str] = []
        mixed_content = False

        async def on_route(route: Any) -> None:
            request = route.request
            if request.resource_type in BLOCKED_RESOURCE_TYPES or BLOCKED_URL_PATTERNS.search(
                request.url
            ):
                await route.abort()
                return
            await route.continue_()

        def on_response(response: Any) -> None:
            nonlocal transfer_bytes, request_count, mixed_content
            request_count += 1
            with contextlib.suppress(Exception):
                length = response.headers.get("content-length")
                if length and length.isdigit():
                    transfer_bytes += int(length)
            if url.startswith("https://") and response.url.startswith("http://"):
                mixed_content = True

        def on_console(message: Any) -> None:
            if message.type == "error" and len(console_errors) < 15:
                console_errors.append(message.text[:200])

        def on_request_failed(request: Any) -> None:
            if len(failed) < 15:
                failed.append(f"{request.url[:160]} ({request.failure or 'failed'})")

        try:
            await context.route("**/*", on_route)
            page = await context.new_page()
            page.on("response", on_response)
            page.on("console", on_console)
            page.on("requestfailed", on_request_failed)

            started = asyncio.get_event_loop().time()
            # `domcontentloaded` rather than `networkidle`: many small-business
            # sites have a tracking pixel or chat widget that polls forever, so
            # networkidle never fires and every audit burns its full timeout.
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            result.load_time_ms = int((asyncio.get_event_loop().time() - started) * 1000)

            # Give client-rendered frameworks a moment to paint.
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("load", timeout=6000)
            await asyncio.sleep(1.2)

            result.final_url = page.url
            probe = await page.evaluate(_LAYOUT_PROBE)

            result.rendered_html = probe.get("html", "") or ""
            result.scroll_width = probe.get("scrollWidth")
            result.client_width = probe.get("clientWidth")
            if result.scroll_width and result.client_width:
                # 5px of slack: sub-pixel rounding and scrollbar widths produce
                # false positives at exactly 0 tolerance.
                result.overflow_px = max(0, result.scroll_width - result.client_width)
                result.horizontal_overflow = result.overflow_px > 5
            result.smallest_font_px = probe.get("smallestFont")
            result.tiny_text_ratio = round(float(probe.get("tinyRatio") or 0), 3)
            result.tap_target_issues = int(probe.get("tapIssues") or 0)
            result.font_families = probe.get("fontFamilies") or []
            result.color_count = int(probe.get("colorCount") or 0)
            result.above_fold_text_chars = int(probe.get("aboveFoldTextChars") or 0)
            result.has_hero_image = bool(probe.get("hasHeroImage"))

            result.request_count = request_count
            result.transfer_kb = round(transfer_bytes / 1024, 1)
            result.console_errors = console_errors
            result.failed_requests = failed
            result.mixed_content = mixed_content

            if screenshots:
                result.screenshot_mobile_path = await self._screenshot(page, url, "mobile")
                await page.set_viewport_size(DESKTOP_VIEWPORT)
                await asyncio.sleep(0.6)
                result.screenshot_path = await self._screenshot(page, url, "desktop")

            result.ok = True
            _ = response
            self.renders += 1
            return result

        finally:
            with contextlib.suppress(Exception):
                await context.close()

    async def _screenshot(self, page: Any, url: str, label: str) -> str | None:
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        path = self.screenshot_dir / f"{digest}_{label}.jpg"
        try:
            await page.screenshot(
                path=str(path),
                # Above-the-fold only. A full-page shot of a long site is
                # megabytes, and the first screen is what sells the redesign.
                full_page=False,
                type="jpeg",
                quality=72,
                timeout=15000,
            )
            return str(path)
        except Exception as exc:  # noqa: BLE001
            log.debug("browser.screenshot_failed", url=url, error=str(exc))
            return None


class NullBrowserAuditor:
    """Stand-in used when browser auditing is disabled.

    Keeps the auditor's call sites free of ``if browser is not None`` branches.
    """

    renders = 0
    failures = 0

    async def __aenter__(self) -> NullBrowserAuditor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def render(self, url: str, **kwargs: Any) -> RenderResult:
        return RenderResult(ok=False, error="browser auditing disabled")
