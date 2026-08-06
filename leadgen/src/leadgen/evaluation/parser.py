"""Static HTML analysis.

Everything measurable without running JavaScript. This is the cheap tier of the
audit: it runs on every site, costs one HTTP request, and produces most of the
signals that actually drive the sales conversation.

The design-era heuristics are the interesting part. "Outdated" is not a vibe —
it has concrete markers. A page using ``<table>`` for layout, ``<font>`` tags,
or a frameset was built before roughly 2005 and has never been touched. A page
with no ``<meta name="viewport">`` predates responsive design (2012+). These are
falsifiable observations you can put in an email to the owner, which is exactly
what makes them useful.

selectolax is used instead of BeautifulSoup because it is a C parser that is
roughly 20x faster; at 5,000 sites per night that is the difference between
minutes and an hour.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from leadgen.enrichment.normalize import extract_emails, normalize_phone

# --- detection tables --------------------------------------------------------

# Ordered: first match wins for reporting the "primary" platform.
TECH_SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("WordPress", re.compile(r"wp-content|wp-includes|/wp-json/", re.I)),
    ("Shopify", re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.I)),
    ("Wix", re.compile(r"wix\.com|wixstatic|X-Wix-", re.I)),
    ("Squarespace", re.compile(r"squarespace\.com|static1\.squarespace", re.I)),
    ("Weebly", re.compile(r"weebly\.com|weeblycloud", re.I)),
    ("GoDaddy Website Builder", re.compile(r"godaddysites\.com|img1\.wsimg\.com", re.I)),
    ("Duda", re.compile(r"dudamobile|duda_website", re.I)),
    ("Webflow", re.compile(r"webflow\.(?:com|io)|data-wf-page", re.I)),
    ("Joomla", re.compile(r"/components/com_|Joomla!", re.I)),
    ("Drupal", re.compile(r"drupal\.js|/sites/default/files/", re.I)),
    # `/_next/static/` is the Next.js build output path and is by far the most
    # reliable React signal on a production build, where the library name never
    # appears in the bundle filename.
    (
        "React",
        re.compile(
            r"__NEXT_DATA__|/_next/static|react(?:-dom)?(?:\.production)?(?:\.min)?\.js|data-reactroot",
            re.I,
        ),
    ),
    ("Vue", re.compile(r"vue(?:\.runtime)?(?:\.min)?\.js|data-v-[0-9a-f]{8}", re.I)),
    ("Angular", re.compile(r"ng-version|angular(?:\.min)?\.js", re.I)),
    ("Bootstrap", re.compile(r"bootstrap(?:\.min)?\.(?:css|js)", re.I)),
    ("jQuery", re.compile(r"jquery[.-]?([0-9]+\.[0-9]+(?:\.[0-9]+)?)?(?:\.min)?\.js", re.I)),
    ("Elementor", re.compile(r"elementor", re.I)),
    ("Divi", re.compile(r"et_divi|/themes/Divi/", re.I)),
    ("Google Sites", re.compile(r"sites\.google\.com|gstatic\.com/_/mss/", re.I)),
]

# Anything here means the site predates the modern web by a decade or more.
LEGACY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("frameset", re.compile(r"<frameset|<frame\s", re.I)),
    ("font_tag", re.compile(r"<font[\s>]", re.I)),
    ("center_tag", re.compile(r"<center[\s>]", re.I)),
    ("marquee", re.compile(r"<marquee", re.I)),
    ("blink", re.compile(r"<blink", re.I)),
    ("flash", re.compile(r"\.swf|application/x-shockwave-flash|<embed[^>]+flash", re.I)),
    ("applet", re.compile(r"<applet", re.I)),
    ("bgcolor_attr", re.compile(r"<(?:body|table|td|tr)[^>]+bgcolor=", re.I)),
    ("spacer_gif", re.compile(r"spacer\.gif|pixel\.gif|transparent\.gif", re.I)),
    ("frontpage", re.compile(r"Microsoft FrontPage|_vti_", re.I)),
    ("dreamweaver_mm", re.compile(r"MM_swapImage|MM_preloadImages", re.I)),
    ("hit_counter", re.compile(r"hit ?counter|visitor ?counter", re.I)),
]

SOCIAL_PATTERNS = {
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/[^\"'\s>]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[^\"'\s>]+", re.I),
    "twitter": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[^\"'\s>]+", re.I),
    "linkedin": re.compile(r"https?://(?:www\.)?linkedin\.com/[^\"'\s>]+", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/[^\"'\s>]+", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@[^\"'\s>]+", re.I),
    "yelp": re.compile(r"https?://(?:www\.)?yelp\.com/biz/[^\"'\s>]+", re.I),
    "pinterest": re.compile(r"https?://(?:www\.)?pinterest\.com/[^\"'\s>]+", re.I),
}

CTA_PHRASES = re.compile(
    r"\b(get\s+(?:a\s+)?(?:free\s+)?(?:quote|estimate|started|in\s+touch)|"
    r"book\s+(?:now|online|(?:an\s+)?appointment)|schedule\s+(?:a\s+)?(?:call|consultation|service)|"
    r"request\s+(?:a\s+)?(?:quote|estimate|appointment|info)|contact\s+us|call\s+(?:us\s+)?(?:now|today)|"
    r"free\s+(?:consultation|estimate|quote|inspection)|shop\s+now|order\s+online|"
    r"sign\s+up|subscribe|learn\s+more|make\s+(?:an\s+)?appointment)\b",
    re.I,
)

# The en and em dashes are intentional: real copyright lines write ranges as
# "2019–2024" as often as "2019-2024".
COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–—]\s*)?(\d{4})",
    re.I,
)

PARKED_MARKERS = re.compile(
    r"(this domain (?:is|may be) for sale|buy this domain|domain (?:parking|is parked)|"
    r"future home of|coming soon|under construction|site is being (?:built|updated)|"
    r"godaddy\.com/domains|sedoparking|parkingcrew|bodis\.com|hugedomains)",
    re.I,
)

MINIMAL_CONTENT_THRESHOLD = 120  # words


@dataclass(slots=True)
class ParsedPage:
    """Structured facts extracted from one HTML document."""

    url: str

    title: str | None = None
    meta_description: str | None = None
    canonical_url: str | None = None
    lang: str | None = None
    h1_count: int = 0
    h1_texts: list[str] = field(default_factory=list)
    heading_levels: dict[str, int] = field(default_factory=dict)
    heading_outline_ok: bool = False

    has_viewport_meta: bool = False
    viewport_content: str | None = None
    has_favicon: bool = False
    has_open_graph: bool = False
    has_twitter_card: bool = False
    structured_data_types: list[str] = field(default_factory=list)

    word_count: int = 0
    text_sample: str = ""
    copyright_year: int | None = None

    total_images: int = 0
    images_missing_alt: int = 0
    total_links: int = 0
    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)

    has_contact_form: bool = False
    contact_page_url: str | None = None
    form_count: int = 0
    has_email_input: bool = False
    has_click_to_call: bool = False
    has_cta: bool = False
    cta_samples: list[str] = field(default_factory=list)
    has_map_embed: bool = False
    has_analytics: bool = False

    tech_stack: list[str] = field(default_factory=list)
    legacy_markers: list[str] = field(default_factory=list)
    uses_table_layout: bool = False
    inline_style_count: int = 0
    is_parked: bool = False
    is_minimal: bool = False

    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: dict[str, str] = field(default_factory=dict)

    design_era: str = "unknown"
    modernity_score: float = 50.0


def parse_html(html: str, url: str) -> ParsedPage:
    """Extract everything statically observable from one page."""
    page = ParsedPage(url=url)
    if not html or not html.strip():
        page.is_minimal = True
        page.design_era = "empty"
        page.modernity_score = 0.0
        return page

    tree = HTMLParser(html)
    lower_html = html.lower()

    _parse_head(tree, page, url)
    _parse_content(tree, page)
    _parse_links(tree, page, url)
    _parse_forms(tree, page)
    _detect_tech(html, lower_html, page)
    _extract_contacts(tree, html, page)

    page.is_parked = bool(PARKED_MARKERS.search(page.text_sample[:2000]))
    page.is_minimal = page.word_count < MINIMAL_CONTENT_THRESHOLD
    page.design_era, page.modernity_score = _estimate_era(page)
    return page


# -----------------------------------------------------------------------------


def _parse_head(tree: HTMLParser, page: ParsedPage, url: str) -> None:
    if node := tree.css_first("title"):
        page.title = (node.text() or "").strip() or None

    if html_node := tree.css_first("html"):
        page.lang = html_node.attributes.get("lang")

    for meta in tree.css("meta"):
        attrs = meta.attributes
        name = (attrs.get("name") or "").lower()
        prop = (attrs.get("property") or "").lower()
        content = attrs.get("content") or ""

        if name == "description":
            page.meta_description = content.strip() or None
        elif name == "viewport":
            page.has_viewport_meta = True
            page.viewport_content = content
        elif prop.startswith("og:"):
            page.has_open_graph = True
        elif name.startswith("twitter:"):
            page.has_twitter_card = True

    for link in tree.css("link"):
        rel = (link.attributes.get("rel") or "").lower()
        if "canonical" in rel:
            page.canonical_url = link.attributes.get("href")
        if "icon" in rel:
            page.has_favicon = True

    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text() or "{}")
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and (schema_type := item.get("@type")):
                types = schema_type if isinstance(schema_type, list) else [schema_type]
                page.structured_data_types.extend(str(t) for t in types)

    _ = url


def _parse_content(tree: HTMLParser, page: ParsedPage) -> None:
    for level in range(1, 7):
        nodes = tree.css(f"h{level}")
        page.heading_levels[f"h{level}"] = len(nodes)
        if level == 1:
            page.h1_count = len(nodes)
            page.h1_texts = [(n.text() or "").strip()[:200] for n in nodes]

    # A sane outline has exactly one H1 and does not jump from H1 straight to
    # H4 — both are real accessibility and SEO problems, not style opinions.
    has_single_h1 = page.h1_count == 1
    no_gap = not (page.heading_levels.get("h3", 0) and not page.heading_levels.get("h2", 0))
    page.heading_outline_ok = has_single_h1 and no_gap

    body = tree.css_first("body")
    if body:
        for tag in ("script", "style", "noscript", "template"):
            for node in body.css(tag):
                node.decompose()
        text = re.sub(r"\s+", " ", body.text() or "").strip()
    else:
        text = ""
    page.text_sample = text[:20000]
    page.word_count = len(text.split())

    if match := COPYRIGHT_RE.search(text[-4000:] or text):
        year = int(match.group(1))
        # Guard against phone numbers and addresses matching as years.
        if 1990 <= year <= datetime.utcnow().year + 1:
            page.copyright_year = year

    images = tree.css("img")
    page.total_images = len(images)
    page.images_missing_alt = sum(
        1 for img in images if not (img.attributes.get("alt") or "").strip()
    )

    page.inline_style_count = len(tree.css("[style]"))

    # Table layout: a table containing another table, or a table with no
    # semantic markup, is a layout table rather than a data table.
    tables = tree.css("table")
    if tables:
        nested = any(t.css("table") for t in tables)
        semantic = any(t.css("th") or t.css("caption") or t.css("thead") for t in tables)
        page.uses_table_layout = nested or (len(tables) >= 3 and not semantic)

    page.has_map_embed = bool(
        tree.css_first('iframe[src*="google.com/maps"]')
        or tree.css_first('iframe[src*="maps.google"]')
        or tree.css_first('iframe[src*="openstreetmap"]')
    )


def _parse_links(tree: HTMLParser, page: ParsedPage, url: str) -> None:
    base_host = urlparse(url).netloc.lower().removeprefix("www.")
    internal: list[str] = []
    external: list[str] = []

    for anchor in tree.css("a"):
        href = (anchor.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "data:")):
            if href.startswith("tel:"):
                page.has_click_to_call = True
            continue
        if href.startswith("tel:"):
            page.has_click_to_call = True
            continue

        absolute = urljoin(url, href)
        host = urlparse(absolute).netloc.lower().removeprefix("www.")
        (internal if host == base_host or not host else external).append(absolute)

        text = (anchor.text() or "").strip()
        if text and CTA_PHRASES.search(text):
            page.has_cta = True
            if len(page.cta_samples) < 5:
                page.cta_samples.append(text[:80])

    page.total_links = len(internal) + len(external)
    # Cap stored links: a 4,000-link sitemap page would otherwise blow up the
    # JSONB column and the broken-link stage's workload.
    page.internal_links = list(dict.fromkeys(internal))[:150]
    page.external_links = list(dict.fromkeys(external))[:60]

    if not page.has_cta:
        for button in tree.css("button, input[type=submit], .btn, .button"):
            label = (button.text() or button.attributes.get("value") or "").strip()
            if label and CTA_PHRASES.search(label):
                page.has_cta = True
                page.cta_samples.append(label[:80])
                break


def _parse_forms(tree: HTMLParser, page: ParsedPage) -> None:
    forms = tree.css("form")
    page.form_count = len(forms)

    for form in forms:
        inputs = form.css("input, textarea, select")
        types = {(i.attributes.get("type") or "text").lower() for i in inputs}
        names = " ".join(
            (i.attributes.get("name") or "") + (i.attributes.get("id") or "") for i in inputs
        ).lower()

        if "email" in types or "email" in names:
            page.has_email_input = True

        # Distinguish a contact form from a search box or newsletter signup:
        # a real contact form has a message field or several inputs.
        has_textarea = bool(form.css("textarea"))
        looks_like_search = "search" in names and len(inputs) <= 2
        if looks_like_search:
            continue
        if has_textarea or (len(inputs) >= 3 and page.has_email_input):
            page.has_contact_form = True

    # Many small sites keep the form on /contact rather than the homepage. A
    # link is not a form, so this never sets has_contact_form — it tells the
    # auditor there is a second page worth fetching before concluding "no way
    # to contact them online".
    for anchor in tree.css("a"):
        href = (anchor.attributes.get("href") or "").lower()
        text = (anchor.text() or "").lower().strip()
        if "contact" in href or text in {"contact", "contact us", "get in touch"}:
            page.contact_page_url = urljoin(page.url, anchor.attributes.get("href") or "")
            break


def _detect_tech(html: str, lower_html: str, page: ParsedPage) -> None:
    for name, pattern in TECH_SIGNATURES:
        if pattern.search(html):
            page.tech_stack.append(name)

    for name, pattern in LEGACY_MARKERS:
        if pattern.search(html):
            page.legacy_markers.append(name)

    page.has_analytics = any(
        marker in lower_html
        for marker in (
            "googletagmanager.com",
            "google-analytics.com",
            "gtag(",
            "ga(",
            "plausible.io",
            "fathom",
            "matomo",
            "hotjar",
            "clarity.ms",
        )
    )


def _extract_contacts(tree: HTMLParser, html: str, page: ParsedPage) -> None:
    # mailto: links are higher-confidence than regex hits on body text.
    mailto: list[str] = []
    for anchor in tree.css('a[href^="mailto:"]'):
        href = anchor.attributes.get("href") or ""
        address = href[7:].split("?")[0].strip().lower()
        if address and "@" in address:
            mailto.append(address)

    page.emails = list(dict.fromkeys(mailto + extract_emails(page.text_sample)))[:10]

    tel_numbers: list[str] = []
    for anchor in tree.css('a[href^="tel:"]'):
        raw = (anchor.attributes.get("href") or "")[4:]
        if normalized := normalize_phone(raw):
            tel_numbers.append(normalized)

    text_phones = re.findall(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", page.text_sample)
    for candidate in text_phones[:10]:
        if normalized := normalize_phone(candidate):
            tel_numbers.append(normalized)
    page.phones = list(dict.fromkeys(tel_numbers))[:5]

    for platform, pattern in SOCIAL_PATTERNS.items():
        if match := pattern.search(html):
            link = match.group(0).rstrip("\"'/)")
            # Skip share widgets — sharer.php is not the business's page.
            if "sharer" in link or "/share" in link or "intent/tweet" in link:
                continue
            page.social_links[platform] = link[:300]


def _estimate_era(page: ParsedPage) -> tuple[str, float]:
    """Classify how old the site *looks*, and score its modernity 0-100.

    The output is used twice: as a design-quality input to the website score,
    and verbatim in the pitch ("your site looks like it was built around
    2008"), so the labels are written to be shown to a human.
    """
    score = 60.0
    era = "modern"

    hard_legacy = {"frameset", "font_tag", "flash", "applet", "marquee", "blink", "frontpage"}
    if hard_legacy & set(page.legacy_markers):
        return "pre-2005 (legacy HTML)", 5.0

    if page.uses_table_layout:
        era = "2000s (table layout)"
        score = 15.0
    elif not page.has_viewport_meta:
        # Responsive design became standard around 2012. No viewport meta tag
        # means the site was never built for phones — which is most of the
        # traffic for every niche in this system.
        era = "pre-2012 (not responsive)"
        score = 25.0
    else:
        modern_signals = 0
        if page.has_open_graph:
            modern_signals += 1
        if page.structured_data_types:
            modern_signals += 1
        if any(t in page.tech_stack for t in ("React", "Vue", "Angular", "Webflow", "Squarespace")):
            modern_signals += 2
        if page.has_analytics:
            modern_signals += 1
        if page.has_favicon:
            modern_signals += 1
        if page.social_links:
            modern_signals += 1

        score = 45.0 + modern_signals * 8
        if modern_signals >= 5:
            era = "current (2020s)"
        elif modern_signals >= 3:
            era = "recent (2015-2020)"
        else:
            era = "dated (2012-2015)"

    if page.legacy_markers:
        score -= 5 * len(page.legacy_markers)
    if page.copyright_year:
        stale_years = datetime.utcnow().year - page.copyright_year
        if stale_years >= 5:
            score -= 15
            era += f", copyright {page.copyright_year}"
        elif stale_years >= 2:
            score -= 7
    if page.is_minimal:
        score -= 10

    return era, max(0.0, min(100.0, score))
