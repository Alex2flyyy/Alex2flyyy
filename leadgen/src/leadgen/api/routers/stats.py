"""Analytics endpoints backing the dashboard."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query

from leadgen.api.deps import DbSession
from leadgen.api.schemas import StatsOut
from leadgen.db.repositories import AnalyticsRepository, AuditRepository

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("", response_model=StatsOut, summary="Everything the dashboard needs")
async def all_stats(session: DbSession, day: date | None = None) -> StatsOut:
    repo = AnalyticsRepository(session)
    return StatsOut(
        overview=await repo.overview(day),
        by_city=await repo.by_city(),
        by_niche=await repo.by_niche(),
        by_website_status=await repo.by_website_status(),
        by_status=await repo.by_status(),
        score_distribution=await repo.score_distribution(),
        daily_trend=await repo.daily_trend(),
        funnel=await repo.conversion_funnel(),
    )


@router.get("/overview", summary="Headline counters")
async def overview(session: DbSession, day: date | None = None) -> dict[str, Any]:
    return await AnalyticsRepository(session).overview(day)


@router.get("/by-city", summary="Leads grouped by city")
async def by_city(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 20
) -> list[dict[str, Any]]:
    return await AnalyticsRepository(session).by_city(limit=limit)


@router.get("/by-niche", summary="Leads grouped by niche")
async def by_niche(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 40
) -> list[dict[str, Any]]:
    return await AnalyticsRepository(session).by_niche(limit=limit)


@router.get("/funnel", summary="Discovery to won conversion funnel")
async def funnel(session: DbSession) -> list[dict[str, Any]]:
    return await AnalyticsRepository(session).conversion_funnel()


@router.get("/website-changes", summary="Websites whose status recently changed")
async def website_changes(
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 7,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """Recent website status transitions.

    A prospect whose site just broke is the warmest call available; a prospect
    whose site just got good was sold by someone else. Both are actionable.
    """
    changes = await AuditRepository(session).recent_changes(days=days, limit=limit)
    return [
        {
            "business_id": change.business_id,
            "business_name": change.business.name if change.business else None,
            "old_status": change.old_status.value if change.old_status else None,
            "new_status": change.new_status.value,
            "old_score": change.old_score,
            "new_score": change.new_score,
            "direction": _direction(change.old_score, change.new_score),
            "detected_at": change.detected_at.isoformat(),
        }
        for change in changes
    ]


def _direction(old: float | None, new: float | None) -> str:
    if old is None or new is None:
        return "new"
    if new > old + 5:
        return "improved"
    if new < old - 5:
        return "degraded"
    return "unchanged"
