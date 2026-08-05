"""Stage helpers for the daily pipeline.

Split out so each stage is independently testable and so ``daily.py`` reads as
a sequence of named steps rather than a thousand-line function.

A note on transaction boundaries: every stage opens its own short session and
commits in batches. A single transaction spanning a two-hour run would hold
locks, bloat the WAL, and lose everything on one failure at minute 118.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from leadgen.db.models import Business, WebsiteAuditRecord
from leadgen.db.repositories import (
    AuditRepository,
    BusinessRepository,
    ContactRepository,
    InsightRepository,
    LeadRepository,
)
from leadgen.dedupe.matcher import build_identity
from leadgen.domain import AIEnrichment, RawBusiness, ScoreResult, WebsiteAudit
from leadgen.enrichment.normalize import format_phone_display, normalize_state, normalize_zip
from leadgen.logging import get_logger
from leadgen.scoring.lead_score import _is_chain  # noqa: PLC2701 - intentional internal reuse

log = get_logger(__name__)


def business_values(raw: RawBusiness, *, location_key: str | None, scoring_config: Any) -> dict:
    """Map a provider record onto Business column values."""
    identity = build_identity(raw)
    return {
        "source": raw.source,
        "source_id": raw.source_id,
        "source_key": identity["source_key"],
        "dedupe_key": identity["dedupe_key"],
        "name": raw.name[:400],
        "normalized_name": identity["normalized_name"],
        "description": raw.description,
        "phone": format_phone_display(identity["phone_e164"]) or raw.phone,
        "phone_e164": identity["phone_e164"],
        "street_address": raw.street_address,
        "city": raw.city,
        "county": (raw.raw or {}).get("county"),
        "state": normalize_state(raw.state),
        "zip_code": normalize_zip(raw.zip_code),
        "country": raw.country or "US",
        "lat": raw.lat,
        "lng": raw.lng,
        "google_place_id": raw.source_id if raw.source == "google_places" else None,
        "google_maps_url": raw.google_maps_url,
        "website_url": raw.website,
        "website_domain": identity["website_domain"],
        "niche_key": raw.niche_key,
        "categories": raw.categories[:10],
        "rating": raw.rating,
        "review_count": raw.review_count,
        "price_level": raw.price_level,
        "hours": raw.hours or {},
        "business_status": raw.business_status,
        "is_chain": _is_chain(raw.name, identity["website_domain"], scoring_config),
        "discovery_location": location_key,
        "raw_payload": {
            k: v for k, v in (raw.raw or {}).items() if k in {"map_rank", "map_query", "osm_tags"}
        },
    }


async def persist_businesses(
    session: Any,
    records: list[RawBusiness],
    *,
    location_key: str | None,
    scoring_config: Any,
) -> tuple[list[int], int, int]:
    """Upsert a batch. Returns ``(business_ids, created_count, duplicate_count)``."""
    repo = BusinessRepository(session)
    ids: list[int] = []
    created = 0
    duplicates = 0

    for raw in records:
        if not raw.name:
            continue
        values = business_values(raw, location_key=location_key, scoring_config=scoring_config)
        try:
            business, was_created = await repo.upsert(values)
        except Exception as exc:  # noqa: BLE001
            # The dedupe_key unique constraint fires when the same business
            # arrives under two different provider ids. That is the constraint
            # doing its job, not an error worth aborting the batch for.
            log.debug("persist.conflict", name=raw.name, error=str(exc)[:200])
            await session.rollback()
            duplicates += 1
            continue

        ids.append(business.id)
        if was_created:
            created += 1
        else:
            duplicates += 1

    return ids, created, duplicates


async def save_audit(
    session: Any,
    business: Business,
    audit: WebsiteAudit,
    run_id: int | None,
) -> WebsiteAuditRecord:
    """Persist an audit and record a status transition if one occurred."""
    audit_repo = AuditRepository(session)
    previous = await audit_repo.latest_for_business(business.id)

    record = WebsiteAuditRecord(
        business_id=business.id,
        run_id=run_id,
        url=audit.url,
        final_url=audit.final_url,
        outcome=audit.outcome,
        error=audit.error,
        http_status=audit.http_status,
        response_time_ms=audit.response_time_ms,
        uses_https=audit.uses_https,
        ssl_valid=audit.ssl_valid,
        ssl_expires_at=audit.ssl_expires_at,
        mixed_content=audit.mixed_content,
        mobile_friendly=audit.mobile_friendly,
        has_viewport_meta=audit.has_viewport_meta,
        horizontal_overflow=audit.horizontal_overflow,
        psi_performance=audit.psi_performance,
        psi_seo=audit.psi_seo,
        psi_accessibility=audit.psi_accessibility,
        psi_best_practices=audit.psi_best_practices,
        lcp_ms=audit.lcp_ms,
        cls=audit.cls,
        tbt_ms=audit.tbt_ms,
        page_weight_kb=audit.page_weight_kb,
        title=audit.title,
        meta_description=audit.meta_description,
        h1_count=audit.h1_count,
        word_count=audit.word_count,
        copyright_year=audit.copyright_year,
        has_structured_data=audit.has_structured_data,
        has_sitemap=audit.has_sitemap,
        has_contact_form=audit.has_contact_form,
        has_click_to_call=audit.has_click_to_call,
        has_cta=audit.has_cta,
        broken_link_count=audit.broken_link_count,
        images_missing_alt=audit.images_missing_alt,
        tech_stack=audit.tech_stack[:20],
        design_era=audit.design_era,
        screenshot_path=audit.screenshot_path,
        screenshot_mobile_path=audit.screenshot_mobile_path,
        dimension_scores=audit.dimension_scores,
        signals=[asdict(s) for s in audit.signals][:60],
        problems=audit.problems,
        score=audit.score,
        status=audit.status,
        checked_at=audit.checked_at,
        duration_ms=audit.duration_ms,
    )
    await audit_repo.add(record)
    await audit_repo.record_status_change(
        business_id=business.id,
        old=previous,
        new_status=audit.status,
        new_score=audit.score,
        new_url=audit.final_url or audit.url,
    )
    await BusinessRepository(session).touch_checked(business.id)
    return record


async def save_lead(
    session: Any,
    business: Business,
    result: ScoreResult,
    audit: WebsiteAudit | None,
    run_id: int | None,
) -> tuple[Any, bool]:
    from leadgen.scoring.lead_score import website_status_for

    return await LeadRepository(session).upsert_score(
        business_id=business.id,
        score=result.score,
        website_status=website_status_for(business, audit),
        website_score=audit.score if audit else None,
        qualified=result.qualified,
        reason=" • ".join(result.reasons[:5]) if result.reasons else None,
        components=[asdict(c) for c in result.components],
        adjustments=[
            {"key": key, "delta": delta, "reason": why} for key, delta, why in result.adjustments
        ],
        run_id=run_id,
    )


async def save_contacts(session: Any, business_id: int, rows: list[dict[str, Any]]) -> int:
    return await ContactRepository(session).upsert_many(business_id, rows)


async def save_insight(
    session: Any,
    business_id: int,
    enrichment: AIEnrichment,
    input_hash: str,
    run_id: int | None,
) -> None:
    from leadgen.db.models import AIInsight

    await InsightRepository(session).add(
        AIInsight(
            business_id=business_id,
            run_id=run_id,
            summary=enrichment.summary,
            opportunity=enrichment.opportunity,
            redesign_recommendations=enrichment.redesign_recommendations,
            outreach_angle=enrichment.outreach_angle,
            outreach_subject=enrichment.outreach_subject,
            talking_points=enrichment.talking_points,
            inferred_niche=enrichment.inferred_niche,
            estimated_value_tier=enrichment.estimated_value_tier,
            confidence=enrichment.confidence,
            model=enrichment.model,
            tokens_used=enrichment.tokens_used,
            input_hash=input_hash,
        )
    )


def chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def now() -> datetime:
    return datetime.utcnow()
