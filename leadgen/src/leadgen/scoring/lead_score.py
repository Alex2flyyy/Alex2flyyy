"""Lead scoring.

Produces a 0-100 score where **higher means "call this one first"**. It is not
the inverse of the website score, and conflating the two is the classic mistake
in this kind of system: the worst website in town belongs to a business that
went under in 2019 and answers no phone. That is a terrible lead.

Five components, weighted in ``config/scoring.yml``:

* **web_opportunity** — how much room there is to improve. Dominant weight.
* **reachability** — can you actually contact a decision-maker.
* **business_activity** — is this a going concern, from reviews and status.
* **revenue_potential** — will the job be worth doing, from niche and scale.
* **market_pressure** — competitive niches in busy markets close faster,
  because the owner already feels the pain of losing to better-marketed rivals.

Then a set of explicit adjustments for the situations a weighted average
handles badly — a franchise (corporate owns the website, unsellable) or a
business with no contact channel at all (unreachable, therefore unsellable
regardless of how bad the site is).
"""

from __future__ import annotations

import re
from typing import Any

from leadgen.config import Niche, get_scoring
from leadgen.domain import (
    AuditOutcome,
    RawBusiness,
    ScoreComponent,
    ScoreResult,
    WebsiteAudit,
    WebsiteStatus,
)
from leadgen.enrichment.normalize import extract_domain


def score_lead(
    business: RawBusiness | Any,
    audit: WebsiteAudit | None,
    niche: Niche | None,
    *,
    has_email: bool = False,
    has_phone: bool | None = None,
) -> ScoreResult:
    """Score one business. ``business`` may be a RawBusiness or an ORM Business."""
    config = get_scoring()
    lead_config = config.lead_score

    name = getattr(business, "name", "") or ""
    website = getattr(business, "website", None) or getattr(business, "website_url", None)
    rating = getattr(business, "rating", None)
    review_count = getattr(business, "review_count", None) or 0
    phone = getattr(business, "phone", None) or getattr(business, "phone_e164", None)
    status = getattr(business, "business_status", None)
    price_level = getattr(business, "price_level", None)
    hours = getattr(business, "hours", None) or {}

    reachable_by_phone = bool(phone) if has_phone is None else has_phone
    domain = extract_domain(website)

    components = [
        _web_opportunity(audit, website, domain, config),
        _reachability(reachable_by_phone, has_email, audit),
        _business_activity(rating, review_count, status, hours, lead_config.activity),
        _revenue_potential(niche, price_level, review_count),
        _market_pressure(niche, review_count),
    ]

    for component in components:
        component.weight = lead_config.weights.get(component.key, 0.0)
        component.contribution = round(component.raw * component.weight, 2)

    base = sum(c.contribution for c in components)

    adjustments = _adjustments(
        name=name,
        website=website,
        domain=domain,
        audit=audit,
        status=status,
        rating=rating,
        review_count=review_count,
        reachable_by_phone=reachable_by_phone,
        has_email=has_email,
        social_links=getattr(business, "social_links", None) or {},
        hours=hours,
        config=config,
    )

    total = base + sum(delta for _, delta, _ in adjustments)
    score = max(0, min(100, round(total)))

    result = ScoreResult(
        score=score,
        components=components,
        adjustments=adjustments,
        qualified=score >= lead_config.qualification_threshold,
    )
    result.reasons = _reasons(result, audit, website, niche)
    return result


# =============================================================================
# Components
# =============================================================================


def _web_opportunity(
    audit: WebsiteAudit | None, website: str | None, domain: str | None, config: Any
) -> ScoreComponent:
    if not website:
        return ScoreComponent(
            "web_opportunity",
            "Web opportunity",
            100.0,
            0.0,
            0.0,
            "No website at all — the largest possible opportunity",
        )

    if domain and domain in set(config.social_as_website_domains):
        return ScoreComponent(
            "web_opportunity",
            "Web opportunity",
            92.0,
            0.0,
            0.0,
            "Uses a social media page instead of a real website",
        )

    if audit is None:
        return ScoreComponent(
            "web_opportunity",
            "Web opportunity",
            55.0,
            0.0,
            0.0,
            "Website exists but has not been evaluated yet",
        )

    if audit.outcome != AuditOutcome.OK:
        raw = 95.0 if audit.outcome != AuditOutcome.SKIPPED_ROBOTS else 50.0
        return ScoreComponent(
            "web_opportunity",
            "Web opportunity",
            raw,
            0.0,
            0.0,
            audit.problems[0] if audit.problems else "Website is unreachable",
        )

    # The opportunity is the gap between the current site and a good one.
    raw = max(0.0, 100.0 - audit.score)
    return ScoreComponent(
        "web_opportunity",
        "Web opportunity",
        round(raw, 1),
        0.0,
        0.0,
        f"Website scores {audit.score:.0f}/100 ({audit.status.display.lower()})",
    )


def _reachability(has_phone: bool, has_email: bool, audit: WebsiteAudit | None) -> ScoreComponent:
    channels = []
    raw = 0.0
    if has_phone:
        raw += 55.0
        channels.append("phone")
    if has_email:
        raw += 35.0
        channels.append("email")
    if audit and audit.has_contact_form:
        raw += 10.0
        channels.append("contact form")

    if not channels:
        return ScoreComponent(
            "reachability",
            "Reachability",
            0.0,
            0.0,
            0.0,
            "No phone, email, or form found — cannot be contacted",
        )
    return ScoreComponent(
        "reachability",
        "Reachability",
        min(100.0, raw),
        0.0,
        0.0,
        "Reachable by " + ", ".join(channels),
    )


def _business_activity(
    rating: float | None,
    review_count: int,
    status: str | None,
    hours: dict[str, str],
    activity_config: Any,
) -> ScoreComponent:
    if status and status.upper() in {"CLOSED_PERMANENTLY", "PERMANENTLY_CLOSED"}:
        return ScoreComponent(
            "business_activity",
            "Business activity",
            0.0,
            0.0,
            0.0,
            "Listing marked permanently closed",
        )

    raw = 30.0
    notes = []

    if review_count >= activity_config.reviews_saturated:
        raw += 40.0
        notes.append(f"{review_count} reviews — well established")
    elif review_count >= activity_config.reviews_strong:
        raw += 35.0
        notes.append(f"{review_count} reviews — active customer base")
    elif review_count >= activity_config.reviews_floor:
        raw += 20.0
        notes.append(f"{review_count} reviews")
    else:
        # Few reviews cuts both ways: possibly new and hungry, possibly dormant.
        raw += 5.0
        notes.append(f"only {review_count} reviews — low online presence")

    if rating is not None:
        if rating >= 4.5:
            raw += 20.0
            notes.append(f"{rating:.1f}★ rating — customers are happy, marketing is the gap")
        elif rating >= 4.0:
            raw += 15.0
            notes.append(f"{rating:.1f}★ rating")
        elif rating >= 3.0:
            raw += 8.0
            notes.append(f"{rating:.1f}★ rating")
        else:
            notes.append(f"{rating:.1f}★ rating — reputation problems beyond the website")

    if hours:
        raw += 10.0
        notes.append("published hours")

    return ScoreComponent(
        "business_activity",
        "Business activity",
        min(100.0, raw),
        0.0,
        0.0,
        "; ".join(notes),
    )


def _revenue_potential(
    niche: Niche | None, price_level: int | None, review_count: int
) -> ScoreComponent:
    ticket = niche.ticket if niche else 0.5
    raw = ticket * 70.0

    # Review volume is the best free proxy for business size.
    if review_count >= 200:
        raw += 25.0
        scale = "large local operation"
    elif review_count >= 50:
        raw += 18.0
        scale = "established operation"
    elif review_count >= 15:
        raw += 10.0
        scale = "small operation"
    else:
        scale = "very small or new operation"

    if price_level is not None and price_level >= 3:
        raw += 8.0

    label = niche.label if niche else "business"
    return ScoreComponent(
        "revenue_potential",
        "Revenue potential",
        min(100.0, round(raw, 1)),
        0.0,
        0.0,
        f"{label} with high project value" if ticket >= 0.8 else f"{label}, {scale}",
    )


def _market_pressure(niche: Niche | None, review_count: int) -> ScoreComponent:
    if niche is None:
        return ScoreComponent("market_pressure", "Market pressure", 50.0, 0.0, 0.0, "Unknown niche")

    # Demand says customers are searching; competition says rivals are already
    # capturing them. Both raise the urgency of fixing the website.
    raw = (niche.demand * 55.0) + (niche.competition * 45.0)

    note = f"{niche.label}: "
    if niche.demand >= 0.85 and niche.competition >= 0.85:
        note += "customers search online constantly and competitors are already there"
    elif niche.demand >= 0.85:
        note += "strong online search demand"
    elif niche.competition >= 0.85:
        note += "crowded local market"
    else:
        note += "moderate online demand"

    _ = review_count
    return ScoreComponent(
        "market_pressure", "Market pressure", min(100.0, round(raw, 1)), 0.0, 0.0, note
    )


# =============================================================================
# Adjustments
# =============================================================================


def _adjustments(
    *,
    name: str,
    website: str | None,
    domain: str | None,
    audit: WebsiteAudit | None,
    status: str | None,
    rating: float | None,
    review_count: int,
    reachable_by_phone: bool,
    has_email: bool,
    social_links: dict[str, Any],
    hours: dict[str, Any],
    config: Any,
) -> list[tuple[str, float, str]]:
    out: list[tuple[str, float, str]] = []

    def add(key: str, why: str) -> None:
        delta = config.adjustment(key)
        if delta:
            out.append((key, delta, why))

    if status and status.upper() in {"CLOSED_PERMANENTLY", "PERMANENTLY_CLOSED"}:
        add("permanently_closed_penalty", "listing is permanently closed")
        return out  # nothing else matters

    if _is_chain(name, domain, config):
        add("chain_or_franchise_penalty", "appears to be a chain or franchise location")

    if not website:
        add("no_website_bonus", "no website found anywhere")
    else:
        if domain and domain in set(config.social_as_website_domains):
            add("facebook_page_as_website_bonus", "a social page is standing in for a website")
        if domain and any(domain.endswith(d) for d in config.free_builder_domains):
            add("site_builder_free_tier_bonus", "site is on a free website-builder subdomain")

    if audit is not None:
        if audit.outcome in {
            AuditOutcome.DNS_FAILURE,
            AuditOutcome.CONNECTION_REFUSED,
            AuditOutcome.HTTP_ERROR,
            AuditOutcome.TIMEOUT,
            AuditOutcome.PARKED,
        }:
            add("site_down_bonus", "website is down or unreachable")
        if audit.outcome == AuditOutcome.OK:
            if not audit.uses_https or not audit.ssl_valid:
                add("ssl_missing_bonus", "no valid SSL certificate")
            if audit.mobile_friendly is False or audit.horizontal_overflow:
                add("not_mobile_friendly_bonus", "site does not work properly on phones")
            # A site that is genuinely good was probably built recently by
            # someone else. Pitching a redesign here wastes a call.
            if audit.score >= 80 and audit.design_era and "current" in audit.design_era:
                add(
                    "recently_redesigned_penalty",
                    "site looks recently built and is performing well",
                )

    if not reachable_by_phone and not has_email and not (audit and audit.has_contact_form):
        add("no_contact_channel_penalty", "no way to contact this business")

    # The owner who never set their presence up at all: findable and callable,
    # but no site, no social, no hours, barely any reviews. Nobody has sold
    # them anything digital yet, which is exactly why they are worth a call.
    # Gated on having a phone -- an unreachable business is not a lead however
    # thin its listing is, and that case is already penalised above.
    if reachable_by_phone and not website and not social_links and not hours:
        if review_count < config.lead_score.activity.reviews_floor:
            add(
                "unclaimed_listing_bonus",
                "listing is bare -- no site, socials, or hours -- and looks unmanaged",
            )
        else:
            add(
                "thin_presence_bonus",
                "real customers but no website, socials, or hours online",
            )

    if rating is not None and rating >= 4.5 and review_count >= 50:
        add("high_rating_many_reviews_bonus", "thriving business that can afford a redesign")

    return out


def _is_chain(name: str, domain: str | None, config: Any) -> bool:
    for pattern in config.chain_signals.compiled_names:
        if pattern.search(name):
            return True
    if domain:
        chain_domains = set(config.chain_signals.domains)
        # Aggregator domains (DoorDash, Yelp) mean the "website" is someone
        # else's platform, which is the same unsellable situation as a chain.
        if domain in chain_domains and domain not in set(config.social_as_website_domains):
            return True
    return False


def _reasons(
    result: ScoreResult, audit: WebsiteAudit | None, website: str | None, niche: Niche | None
) -> list[str]:
    """The bullet list a salesperson reads before dialing."""
    reasons: list[str] = []

    if not website:
        # Phrased to avoid an article before the niche label, which is plural
        # ("Restaurants") and would read as "a restaurants".
        subject = niche.label.lower() if niche else "local services"
        reasons.append(
            f"No website at all — customers searching for {subject} online will "
            "never find this business"
        )
    elif audit and audit.outcome != AuditOutcome.OK:
        reasons.append(audit.problems[0] if audit.problems else "Website is unreachable")
    elif audit:
        reasons.extend(audit.problems[:4])

    # Lead with the strongest component so the reason list explains the rank.
    for component in sorted(result.components, key=lambda c: -c.contribution)[:2]:
        if component.key != "web_opportunity" and component.explanation:
            reasons.append(component.explanation)

    for _key, delta, why in result.adjustments:
        if delta > 0:
            # Uppercase the first letter only — str.capitalize() would lowercase
            # the rest and turn "no valid SSL certificate" into "...ssl...".
            reasons.append(why[:1].upper() + why[1:])

    seen: set[str] = set()
    deduped = []
    for reason in reasons:
        normalized = reason.lower().strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(reason)
    return deduped[:8]


def website_status_for(business: Any, audit: WebsiteAudit | None) -> WebsiteStatus:
    """Resolve the status stored on the lead."""
    website = getattr(business, "website", None) or getattr(business, "website_url", None)
    if not website:
        return WebsiteStatus.NONE
    if audit is None:
        return WebsiteStatus.UNKNOWN
    return audit.status


_WHITESPACE = re.compile(r"\s+")
