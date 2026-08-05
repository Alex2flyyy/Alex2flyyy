"""Lead endpoints."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from leadgen.api.deps import DbSession, RequireApiKey, audit_payload, lead_payload
from leadgen.api.schemas import (
    LeadDetailOut,
    LeadOut,
    LeadPage,
    LeadUpdateIn,
    SuppressionIn,
)
from leadgen.db.repositories import (
    ActivityRepository,
    LeadRepository,
    SuppressionRepository,
)
from leadgen.domain import LeadStatus, WebsiteStatus

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("", response_model=LeadPage, summary="Search and filter leads")
async def list_leads(
    session: DbSession,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    max_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    status: Annotated[list[LeadStatus] | None, Query()] = None,
    website_status: Annotated[list[WebsiteStatus] | None, Query()] = None,
    niche: Annotated[list[str] | None, Query()] = None,
    city: Annotated[list[str] | None, Query()] = None,
    state: Annotated[list[str] | None, Query()] = None,
    zip_code: Annotated[list[str] | None, Query(alias="zip")] = None,
    qualified_only: bool = False,
    has_phone: bool | None = None,
    has_email: bool | None = None,
    discovered_on: date | None = None,
    discovered_since: date | None = None,
    q: str | None = None,
    order_by: str = "score",
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeadPage:
    repo = LeadRepository(session)
    filters = {
        "min_score": min_score,
        "max_score": max_score,
        "statuses": status,
        "website_statuses": website_status,
        "niches": niche,
        "cities": city,
        "states": state,
        "zips": zip_code,
        "qualified_only": qualified_only,
        "has_phone": has_phone,
        "has_email": has_email,
        "discovered_on": discovered_on,
        "discovered_since": discovered_since,
        "query": q,
    }
    leads = await repo.search(**filters, order_by=order_by, limit=limit, offset=offset)
    total = await repo.count_search(**filters)
    return LeadPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[LeadOut(**lead_payload(lead)) for lead in leads],
    )


@router.get("/today", response_model=list[LeadOut], summary="Today's top prospects")
async def todays_leads(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    day: date | None = None,
) -> list[LeadOut]:
    leads = await LeadRepository(session).top_for_date(day or date.today(), limit=limit)
    return [LeadOut(**lead_payload(lead)) for lead in leads]


@router.get("/follow-ups", response_model=list[LeadOut], summary="Leads due for follow-up")
async def due_follow_ups(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[LeadOut]:
    leads = await LeadRepository(session).due_follow_ups(limit=limit)
    return [LeadOut(**lead_payload(lead)) for lead in leads]


@router.get("/{lead_id}", response_model=LeadDetailOut, summary="Full lead detail")
async def get_lead(lead_id: int, session: DbSession) -> LeadDetailOut:
    lead = await LeadRepository(session).get_full(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"lead {lead_id} not found")

    business = lead.business
    latest_audit = business.audits[0] if business.audits else None
    insight = business.insights[0] if business.insights else None

    return LeadDetailOut(
        **lead_payload(lead),
        score_components=list(lead.score_components or []),
        score_adjustments=list(lead.score_adjustments or []),
        latest_audit=audit_payload(latest_audit) if latest_audit else None,
        insight={
            "summary": insight.summary,
            "opportunity": insight.opportunity,
            "redesign_recommendations": list(insight.redesign_recommendations or []),
            "outreach_angle": insight.outreach_angle,
            "outreach_subject": insight.outreach_subject,
            "talking_points": list(insight.talking_points or []),
            "estimated_value_tier": insight.estimated_value_tier,
            "confidence": insight.confidence,
            "model": insight.model,
        }
        if insight
        else None,
        contacts=[
            {
                "kind": c.kind,
                "value": c.value,
                "label": c.label,
                "person_name": c.person_name,
                "is_primary": c.is_primary,
                "is_generic": c.is_generic,
                "confidence": c.confidence,
            }
            for c in business.contacts
        ],
    )


@router.patch(
    "/{lead_id}",
    response_model=LeadOut,
    dependencies=[RequireApiKey],
    summary="Update lead status, notes, or follow-up",
)
async def update_lead(lead_id: int, payload: LeadUpdateIn, session: DbSession) -> LeadOut:
    repo = LeadRepository(session)
    lead = await repo.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"lead {lead_id} not found")

    if payload.status is not None:
        lead = await repo.update_status(
            lead_id,
            payload.status,
            notes=payload.notes,
            follow_up_at=payload.follow_up_at,
            channel=payload.channel,
            deal_value=payload.deal_value,
            lost_reason=payload.lost_reason,
        )
        assert lead is not None
        if payload.status in {LeadStatus.CONTACTED, LeadStatus.RESPONDED}:
            await ActivityRepository(session).add(
                lead_id,
                payload.channel or "unknown",
                outcome=payload.status.value,
                direction="outbound" if payload.status == LeadStatus.CONTACTED else "inbound",
            )
    else:
        if payload.notes is not None:
            lead.notes = payload.notes
        if payload.follow_up_at is not None:
            lead.follow_up_at = payload.follow_up_at

    await session.flush()
    full = await repo.get_full(lead_id)
    assert full is not None
    return LeadOut(**lead_payload(full))


@router.post(
    "/{lead_id}/suppress",
    status_code=204,
    dependencies=[RequireApiKey],
    summary="Do-not-contact this lead",
)
async def suppress_lead(lead_id: int, session: DbSession, reason: str | None = None) -> None:
    """Mark a lead do-not-contact.

    Suppresses by every identifier we hold, not just the lead id, so the same
    business cannot reappear through a different provider record tomorrow.
    """
    lead = await LeadRepository(session).get_full(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"lead {lead_id} not found")

    repo = SuppressionRepository(session)
    business = lead.business
    reason = reason or "opt-out requested"

    if business.email:
        await repo.add("email", business.email.lower(), reason)
    if business.phone_e164:
        await repo.add("phone", business.phone_e164, reason)
    if business.website_domain:
        await repo.add("domain", business.website_domain.lower(), reason)
    await repo.add("business", business.name.lower(), reason)

    lead.status = LeadStatus.DO_NOT_CONTACT
    lead.qualified = False


@router.post(
    "/suppressions",
    status_code=204,
    dependencies=[RequireApiKey],
    summary="Add an entry to the do-not-contact list",
)
async def add_suppression(payload: SuppressionIn, session: DbSession) -> None:
    await SuppressionRepository(session).add(payload.kind, payload.value.lower(), payload.reason)
