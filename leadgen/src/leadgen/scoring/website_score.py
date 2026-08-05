"""Website quality scoring.

Produces a 0-100 score where **higher is better for the business**. This is the
number you show the prospect ("Google-adjacent tooling scores your site 34/100")
so every point must trace back to a fact you can defend in a phone call.

Structure: eight dimensions, each scored 0-100 from a set of
:class:`~leadgen.domain.Signal` observations, then combined with the weights in
``config/scoring.yml``. Every signal carries a human-readable ``note``, and the
notes for failing signals become the "problems" list that drives the pitch.

A dimension with no observations is *dropped and its weight redistributed*,
rather than scored zero. Penalizing a site because PageSpeed timed out would
put a fabricated problem in a sales email.
"""

from __future__ import annotations

from datetime import datetime

from leadgen.config import get_scoring
from leadgen.domain import AuditOutcome, Signal, WebsiteAudit, WebsiteStatus


def score_website(audit: WebsiteAudit) -> WebsiteAudit:
    """Populate ``score``, ``status``, ``dimension_scores``, ``signals``, ``problems``."""
    config = get_scoring().website_score

    if audit.outcome != AuditOutcome.OK:
        return _score_unreachable(audit)

    signals: list[Signal] = []
    signals += _availability_signals(audit)
    signals += _security_signals(audit)
    signals += _mobile_signals(audit)
    signals += _performance_signals(audit)
    signals += _seo_signals(audit)
    signals += _design_signals(audit)
    signals += _conversion_signals(audit)
    signals += _accessibility_signals(audit)

    audit.signals = signals

    dimension_scores: dict[str, float] = {}
    for dimension in config.weights:
        dimension_signals = [s for s in signals if s.dimension == dimension]
        if not dimension_signals:
            continue
        total_weight = sum(s.weight for s in dimension_signals) or 1.0
        dimension_scores[dimension] = round(
            sum(s.points * s.weight for s in dimension_signals) / total_weight, 1
        )

    audit.dimension_scores = dimension_scores

    # Renormalize over observed dimensions only.
    observed_weight = sum(config.weights[d] for d in dimension_scores) or 1.0
    audit.score = round(
        sum(dimension_scores[d] * config.weights[d] for d in dimension_scores)
        / observed_weight,
        1,
    )

    audit.problems = [s.note for s in signals if s.is_problem and s.note][:15]
    audit.status = _classify(audit, config.good_at_or_above, config.poor_below)
    return audit


def _score_unreachable(audit: WebsiteAudit) -> WebsiteAudit:
    """A site we could not load. The outcome *is* the finding."""
    messages = {
        AuditOutcome.DNS_FAILURE: (
            "Domain does not resolve — the website is effectively gone", 0.0
        ),
        AuditOutcome.CONNECTION_REFUSED: ("Server refuses connections — site is down", 0.0),
        AuditOutcome.TIMEOUT: ("Site did not respond within the timeout — visitors will leave", 8.0),
        AuditOutcome.TLS_ERROR: (
            "SSL certificate is invalid or expired — browsers show a security warning", 10.0
        ),
        AuditOutcome.HTTP_ERROR: (
            f"Site returns an error ({audit.error or 'HTTP error'})", 12.0
        ),
        AuditOutcome.PARKED: ("Domain is parked — there is no real website", 5.0),
        AuditOutcome.BLOCKED: ("Site blocked automated access; could not evaluate", 50.0),
        AuditOutcome.SKIPPED_ROBOTS: ("Not evaluated (robots.txt disallows crawling)", 50.0),
        AuditOutcome.ERROR: ("Website could not be evaluated", 50.0),
    }
    note, score = messages.get(audit.outcome, ("Website could not be evaluated", 50.0))

    audit.score = score
    audit.signals = [
        Signal(
            key=f"outcome_{audit.outcome.value}",
            dimension="availability",
            value=audit.outcome.value,
            points=score,
            note=note,
            is_problem=score < 50,
        )
    ]
    audit.dimension_scores = {"availability": score}
    audit.problems = [note] if score < 50 else []
    audit.status = (
        WebsiteStatus.BROKEN
        if audit.outcome
        in {
            AuditOutcome.DNS_FAILURE,
            AuditOutcome.CONNECTION_REFUSED,
            AuditOutcome.TIMEOUT,
            AuditOutcome.TLS_ERROR,
            AuditOutcome.HTTP_ERROR,
            AuditOutcome.PARKED,
        }
        else WebsiteStatus.UNKNOWN
    )
    return audit


# =============================================================================
# Dimension signal builders
# =============================================================================


def _availability_signals(audit: WebsiteAudit) -> list[Signal]:
    signals = [
        Signal("loads", "availability", True, 100.0, 2.0, "Site loads successfully"),
    ]

    ms = audit.response_time_ms or 0
    if ms > 5000:
        points, note, problem = 10.0, f"Server takes {ms/1000:.1f}s to respond — very slow", True
    elif ms > 2500:
        points, note, problem = 40.0, f"Server responds in {ms/1000:.1f}s — slower than typical", True
    elif ms > 1200:
        points, note, problem = 70.0, f"Server responds in {ms/1000:.1f}s", False
    else:
        points, note, problem = 100.0, f"Server responds quickly ({ms}ms)", False
    signals.append(Signal("ttfb", "availability", ms, points, 1.5, note, problem))

    if audit.checked_link_count:
        ratio = audit.broken_link_count / audit.checked_link_count
        if ratio > 0.25:
            points = 5.0
            note = f"{audit.broken_link_count} of {audit.checked_link_count} links checked are broken"
            problem = True
        elif ratio > 0.05:
            points = 45.0
            note = f"{audit.broken_link_count} broken links found out of {audit.checked_link_count}"
            problem = True
        else:
            points, note, problem = 100.0, "No broken links found in sample", False
        signals.append(
            Signal("broken_links", "availability", audit.broken_link_count, points, 1.5, note, problem)
        )

    if len(audit.redirect_chain) > 2:
        signals.append(
            Signal(
                "redirect_chain", "availability", len(audit.redirect_chain), 40.0, 0.5,
                f"{len(audit.redirect_chain)} redirects before the page loads — slows every visit",
                True,
            )
        )
    return signals


def _security_signals(audit: WebsiteAudit) -> list[Signal]:
    signals: list[Signal] = []

    if not audit.uses_https:
        signals.append(
            Signal(
                "https", "security", False, 0.0, 3.0,
                "No HTTPS — Chrome marks the site 'Not secure' to every visitor",
                True,
            )
        )
    else:
        signals.append(Signal("https", "security", True, 100.0, 3.0, "Served over HTTPS"))

        if not audit.ssl_valid:
            signals.append(
                Signal(
                    "ssl_valid", "security", False, 0.0, 2.0,
                    "SSL certificate does not validate — visitors see a security warning",
                    True,
                )
            )
        elif audit.ssl_days_remaining is not None:
            days = audit.ssl_days_remaining
            if days < 0:
                points, note, problem = 0.0, "SSL certificate has expired", True
            elif days < 15:
                points, note, problem = 30.0, f"SSL certificate expires in {days} days", True
            elif days < 30:
                points, note, problem = 70.0, f"SSL certificate expires in {days} days", False
            else:
                points, note, problem = 100.0, "SSL certificate is valid", False
            signals.append(Signal("ssl_expiry", "security", days, points, 1.0, note, problem))

    if audit.mixed_content:
        signals.append(
            Signal(
                "mixed_content", "security", True, 20.0, 1.0,
                "Page loads insecure resources over HTTP, weakening the padlock",
                True,
            )
        )

    # Scored but never flagged as a "problem": missing security headers are a
    # real weakness, but they belong in a technical audit, not in the four
    # bullet points a business owner reads. Flagging them buries HTTPS and
    # mobile findings that actually sell the job.
    header_count = len(audit.security_headers)
    signals.append(
        Signal(
            "security_headers", "security", header_count,
            min(100.0, header_count * 33.0), 0.5,
            f"{header_count} of 4 common security headers present",
            is_problem=False,
        )
    )
    return signals


def _mobile_signals(audit: WebsiteAudit) -> list[Signal]:
    signals: list[Signal] = []

    if not audit.has_viewport_meta:
        signals.append(
            Signal(
                "viewport", "mobile", False, 0.0, 3.0,
                "No mobile viewport tag — the site was never built for phones",
                True,
            )
        )
    else:
        signals.append(Signal("viewport", "mobile", True, 100.0, 1.5, "Mobile viewport is declared"))

    if audit.horizontal_overflow:
        signals.append(
            Signal(
                "overflow", "mobile", True, 5.0, 3.0,
                "Content runs off the screen on a phone, forcing sideways scrolling",
                True,
            )
        )
    elif audit.mobile_friendly is not None:
        signals.append(
            Signal("overflow", "mobile", False, 100.0, 2.0, "Layout fits a phone screen correctly")
        )

    if audit.smallest_font_px is not None:
        size = audit.smallest_font_px
        if size < 9:
            points, note, problem = 10.0, f"Text as small as {size:.0f}px — unreadable on a phone", True
        elif size < 12:
            points, note, problem = 45.0, f"Some text is only {size:.0f}px — hard to read on mobile", True
        else:
            points, note, problem = 100.0, "Text is legible on mobile", False
        signals.append(Signal("font_size", "mobile", size, points, 1.0, note, problem))

    if audit.tap_target_issues:
        count = audit.tap_target_issues
        points = 20.0 if count > 15 else 55.0 if count > 5 else 85.0
        signals.append(
            Signal(
                "tap_targets", "mobile", count, points, 1.0,
                f"{count} buttons or links are too small to tap reliably",
                count > 5,
            )
        )
    return signals


def _performance_signals(audit: WebsiteAudit) -> list[Signal]:
    signals: list[Signal] = []

    if audit.psi_performance is not None:
        score = audit.psi_performance
        signals.append(
            Signal(
                "psi_performance", "performance", score, score, 3.0,
                f"Google PageSpeed scores this site {score:.0f}/100 on mobile",
                score < 50,
            )
        )

    if audit.lcp_ms is not None:
        # Google's own Core Web Vitals thresholds: good <2.5s, poor >4s.
        lcp = audit.lcp_ms / 1000
        if lcp > 4:
            points, note, problem = 10.0, f"Main content takes {lcp:.1f}s to appear (Google rates >4s as poor)", True
        elif lcp > 2.5:
            points, note, problem = 55.0, f"Main content takes {lcp:.1f}s to appear — above Google's 2.5s target", True
        else:
            points, note, problem = 100.0, f"Main content appears in {lcp:.1f}s", False
        signals.append(Signal("lcp", "performance", audit.lcp_ms, points, 2.0, note, problem))

    if audit.cls is not None:
        if audit.cls > 0.25:
            points, note, problem = 15.0, f"Layout shifts badly while loading (CLS {audit.cls})", True
        elif audit.cls > 0.1:
            points, note, problem = 60.0, f"Some layout shift while loading (CLS {audit.cls})", False
        else:
            points, note, problem = 100.0, "Layout is stable while loading", False
        signals.append(Signal("cls", "performance", audit.cls, points, 1.0, note, problem))

    if audit.page_weight_kb:
        weight = audit.page_weight_kb
        if weight > 5000:
            points, note, problem = 10.0, f"Page weighs {weight/1024:.1f}MB — punishing on mobile data", True
        elif weight > 2500:
            points, note, problem = 45.0, f"Page weighs {weight/1024:.1f}MB — heavier than it should be", True
        elif weight > 1200:
            points, note, problem = 75.0, f"Page weighs {weight:.0f}KB", False
        else:
            points, note, problem = 100.0, f"Page is lightweight ({weight:.0f}KB)", False
        signals.append(Signal("page_weight", "performance", weight, points, 1.0, note, problem))

    return signals


def _seo_signals(audit: WebsiteAudit) -> list[Signal]:
    signals: list[Signal] = []

    if not audit.title:
        signals.append(
            Signal("title", "seo", None, 0.0, 2.0,
                   "No page title — Google has nothing to show in search results", True)
        )
    elif audit.title_length < 15:
        signals.append(
            Signal("title", "seo", audit.title_length, 40.0, 2.0,
                   f"Page title is only {audit.title_length} characters — too thin to rank", True)
        )
    elif audit.title_length > 65:
        signals.append(
            Signal("title", "seo", audit.title_length, 70.0, 2.0,
                   f"Page title is {audit.title_length} characters and will be truncated in search", False)
        )
    else:
        signals.append(Signal("title", "seo", audit.title_length, 100.0, 2.0, "Page title is well-formed"))

    if not audit.meta_description:
        signals.append(
            Signal("meta_description", "seo", None, 10.0, 2.0,
                   "No meta description — Google invents the search snippet instead of you", True)
        )
    elif not 70 <= audit.meta_description_length <= 165:
        signals.append(
            Signal("meta_description", "seo", audit.meta_description_length, 60.0, 2.0,
                   f"Meta description is {audit.meta_description_length} characters "
                   "(70-160 displays best)", False)
        )
    else:
        signals.append(
            Signal("meta_description", "seo", audit.meta_description_length, 100.0, 2.0,
                   "Meta description is well-sized")
        )

    if audit.h1_count == 0:
        signals.append(Signal("h1", "seo", 0, 20.0, 1.5, "No H1 heading on the page", True))
    elif audit.h1_count > 3:
        signals.append(
            Signal("h1", "seo", audit.h1_count, 55.0, 1.5,
                   f"{audit.h1_count} H1 headings dilute the page's topic", True)
        )
    else:
        signals.append(Signal("h1", "seo", audit.h1_count, 100.0, 1.5, "Clear H1 heading present"))

    signals.append(
        Signal("structured_data", "seo", audit.has_structured_data,
               100.0 if audit.has_structured_data else 25.0, 1.5,
               "Structured data present for rich search results"
               if audit.has_structured_data
               else "No structured data — missing rich results and local SEO signals",
               not audit.has_structured_data)
    )

    signals.append(
        Signal("sitemap", "seo", audit.has_sitemap,
               100.0 if audit.has_sitemap else 50.0, 0.8,
               "XML sitemap found" if audit.has_sitemap else "No XML sitemap found",
               not audit.has_sitemap)
    )

    if audit.word_count < 150:
        signals.append(
            Signal("content", "seo", audit.word_count, 15.0, 1.5,
                   f"Only {audit.word_count} words of content — too thin for Google to rank", True)
        )
    elif audit.word_count < 400:
        signals.append(
            Signal("content", "seo", audit.word_count, 60.0, 1.5,
                   f"{audit.word_count} words of content — light for a service business", False)
        )
    else:
        signals.append(
            Signal("content", "seo", audit.word_count, 100.0, 1.5, "Substantial page content")
        )

    if audit.psi_seo is not None:
        signals.append(
            Signal("psi_seo", "seo", audit.psi_seo, audit.psi_seo, 2.0,
                   f"Google's SEO audit scores {audit.psi_seo:.0f}/100", audit.psi_seo < 70)
        )
    return signals


def _design_signals(audit: WebsiteAudit) -> list[Signal]:
    signals: list[Signal] = []

    if audit.is_flash_or_legacy:
        signals.append(
            Signal("legacy_html", "design", True, 0.0, 3.0,
                   "Built with obsolete HTML (Flash, frames, or <font> tags) — "
                   "roughly two decades out of date", True)
        )
    elif audit.uses_table_layout:
        signals.append(
            Signal("table_layout", "design", True, 10.0, 3.0,
                   "Laid out with HTML tables — a pre-2005 technique", True)
        )

    if audit.design_era:
        era_points = {
            "current (2020s)": 95.0,
            "recent (2015-2020)": 78.0,
            "dated (2012-2015)": 45.0,
            "pre-2012 (not responsive)": 20.0,
            "2000s (table layout)": 8.0,
            "pre-2005 (legacy HTML)": 2.0,
            "empty": 0.0,
        }
        points = next(
            (v for k, v in era_points.items() if audit.design_era.startswith(k.split(",")[0])),
            50.0,
        )
        signals.append(
            Signal("design_era", "design", audit.design_era, points, 2.5,
                   f"Design looks {audit.design_era}", points < 50)
        )

    if audit.copyright_year:
        stale = datetime.utcnow().year - audit.copyright_year
        if stale >= 3:
            signals.append(
                Signal("copyright", "design", audit.copyright_year, 20.0, 1.0,
                       f"Copyright notice still says {audit.copyright_year} — "
                       "the site looks abandoned", True)
            )
        elif stale >= 1:
            signals.append(
                Signal("copyright", "design", audit.copyright_year, 70.0, 1.0,
                       f"Copyright year is {audit.copyright_year}", False)
            )

    signals.append(
        Signal("favicon", "design", audit.has_favicon,
               100.0 if audit.has_favicon else 40.0, 0.5,
               "Favicon present" if audit.has_favicon
               else "No favicon — the browser tab shows a blank icon",
               not audit.has_favicon)
    )

    if audit.total_images == 0 and audit.word_count > 50:
        signals.append(
            Signal("images", "design", 0, 25.0, 1.0,
                   "No images at all — the page reads as a wall of text", True)
        )

    if audit.has_open_graph:
        signals.append(
            Signal("open_graph", "design", True, 100.0, 0.5,
                   "Link previews configured for social sharing")
        )
    else:
        signals.append(
            Signal("open_graph", "design", False, 45.0, 0.5,
                   "No social preview tags — shared links look broken on Facebook and iMessage",
                   True)
        )
    return signals


def _conversion_signals(audit: WebsiteAudit) -> list[Signal]:
    signals: list[Signal] = []

    signals.append(
        Signal("contact_form", "conversion", audit.has_contact_form,
               100.0 if audit.has_contact_form else 15.0, 2.5,
               "Contact form available" if audit.has_contact_form
               else "No contact form — visitors have no easy way to reach out",
               not audit.has_contact_form)
    )

    signals.append(
        Signal("click_to_call", "conversion", audit.has_click_to_call,
               100.0 if audit.has_click_to_call else 30.0, 2.0,
               "Phone number is tap-to-call on mobile" if audit.has_click_to_call
               else "Phone number is not tap-to-call — mobile visitors must copy it by hand",
               not audit.has_click_to_call)
    )

    signals.append(
        Signal("cta", "conversion", audit.has_cta,
               100.0 if audit.has_cta else 20.0, 2.0,
               "Clear call to action present" if audit.has_cta
               else "No clear call to action — nothing tells the visitor what to do next",
               not audit.has_cta)
    )

    signals.append(
        Signal("social", "conversion", audit.has_social_links,
               100.0 if audit.has_social_links else 60.0, 0.5,
               "Links to social profiles" if audit.has_social_links
               else "No social media links", False)
    )
    return signals


def _accessibility_signals(audit: WebsiteAudit) -> list[Signal]:
    signals: list[Signal] = []

    if audit.total_images:
        missing_ratio = audit.images_missing_alt / audit.total_images
        if missing_ratio > 0.5:
            points = 15.0
            note = (
                f"{audit.images_missing_alt} of {audit.total_images} images have no alt text — "
                "an accessibility and SEO problem"
            )
            problem = True
        elif missing_ratio > 0.2:
            points, note, problem = 55.0, f"{audit.images_missing_alt} images are missing alt text", True
        else:
            points, note, problem = 100.0, "Images have alt text", False
        signals.append(Signal("alt_text", "accessibility", missing_ratio, points, 2.0, note, problem))

    signals.append(
        Signal("lang", "accessibility", audit.lang_attribute,
               100.0 if audit.lang_attribute else 50.0, 1.0,
               "Page language is declared" if audit.lang_attribute
               else "No language attribute — screen readers cannot pick the right voice",
               not audit.lang_attribute)
    )

    if audit.psi_accessibility is not None:
        signals.append(
            Signal("psi_accessibility", "accessibility", audit.psi_accessibility,
                   audit.psi_accessibility, 2.0,
                   f"Google's accessibility audit scores {audit.psi_accessibility:.0f}/100",
                   audit.psi_accessibility < 70)
        )
    return signals


def _classify(audit: WebsiteAudit, good_at: int, poor_below: int) -> WebsiteStatus:
    """Map a numeric score onto the status shown in reports.

    ``OUTDATED`` is checked before the numeric bands because it is a
    qualitatively different sales conversation from "poor": an outdated site
    means the owner already paid for a website once and it aged out, which is
    a far easier redesign conversation than someone who never valued one.
    """
    if audit.uses_table_layout or audit.is_flash_or_legacy:
        return WebsiteStatus.OUTDATED
    if not audit.has_viewport_meta and audit.score < good_at:
        return WebsiteStatus.OUTDATED
    if audit.score < poor_below:
        return WebsiteStatus.POOR
    if audit.score >= good_at:
        return WebsiteStatus.GOOD
    return WebsiteStatus.AVERAGE


def summarize(audit: WebsiteAudit, limit: int = 4) -> str:
    """One-sentence plain-English verdict for the report and CSV export."""
    if audit.outcome != AuditOutcome.OK:
        return audit.problems[0] if audit.problems else "Website could not be evaluated"
    if not audit.problems:
        return f"Site scores {audit.score:.0f}/100 with no major issues found"
    head = "; ".join(audit.problems[:limit])
    extra = len(audit.problems) - limit
    suffix = f" (+{extra} more issues)" if extra > 0 else ""
    return f"Scores {audit.score:.0f}/100. {head}{suffix}"
