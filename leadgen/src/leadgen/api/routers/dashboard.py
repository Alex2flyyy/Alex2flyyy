"""Server-rendered dashboard.

Jinja templates with a little vanilla JavaScript, no build step. The reasoning:
this dashboard is used by one to three people, it needs to work reliably at
6am, and a Node toolchain would be more maintenance surface than the dashboard
itself. Everything renders server-side from the same repositories the JSON API
uses, so the two can never disagree.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from leadgen.api.deps import DbSession
from leadgen.config import get_niches, get_locations
from leadgen.db.repositories import (
    AnalyticsRepository,
    LeadRepository,
    ReportRepository,
    RunRepository,
)
from leadgen.domain import LeadStatus, WebsiteStatus
from leadgen.enrichment.normalize import format_phone_display

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["phone"] = lambda v: format_phone_display(v) or (v or "—")

router = APIRouter(tags=["dashboard"], include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: DbSession) -> Any:
    analytics = AnalyticsRepository(session)
    lead_repo = LeadRepository(session)

    today = date.today()
    context = {
        "page": "dashboard",
        "overview": await analytics.overview(today),
        "by_city": await analytics.by_city(limit=10),
        "by_niche": await analytics.by_niche(limit=10),
        "by_website_status": await analytics.by_website_status(),
        "by_status": await analytics.by_status(),
        "score_distribution": await analytics.score_distribution(),
        "daily_trend": await analytics.daily_trend(days=30),
        "funnel": await analytics.conversion_funnel(),
        "todays_leads": await lead_repo.top_for_date(today, limit=15),
        "recent_runs": await RunRepository(session).recent(limit=5),
        "recent_reports": await ReportRepository(session).recent(limit=5),
        "today": today,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/leads", response_class=HTMLResponse)
async def leads_page(
    request: Request,
    session: DbSession,
    q: str | None = None,
    niche: str | None = None,
    city: str | None = None,
    state: str | None = None,
    website_status: str | None = None,
    status: str | None = None,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    qualified_only: bool = True,
    order_by: str = "score",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=10, le=200)] = 50,
) -> Any:
    repo = LeadRepository(session)
    filters: dict[str, Any] = {
        "query": q,
        "niches": [niche] if niche else None,
        "cities": [city] if city else None,
        "states": [state] if state else None,
        "website_statuses": [WebsiteStatus(website_status)] if website_status else None,
        "statuses": [LeadStatus(status)] if status else None,
        "min_score": min_score,
        "qualified_only": qualified_only,
    }

    offset = (page - 1) * per_page
    leads = await repo.search(**filters, order_by=order_by, limit=per_page, offset=offset)
    total = await repo.count_search(**filters)

    analytics = AnalyticsRepository(session)
    context = {
        "page": "leads",
        "leads": leads,
        "total": total,
        "page_num": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "filters": {
            "q": q or "",
            "niche": niche or "",
            "city": city or "",
            "state": state or "",
            "website_status": website_status or "",
            "status": status or "",
            "min_score": min_score or "",
            "qualified_only": qualified_only,
            "order_by": order_by,
        },
        "niches": get_niches().niches,
        "cities": await analytics.by_city(limit=60),
        "website_statuses": list(WebsiteStatus),
        "lead_statuses": list(LeadStatus),
    }
    return templates.TemplateResponse(request, "leads.html", context)


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(lead_id: int, request: Request, session: DbSession) -> Any:
    lead = await LeadRepository(session).get_full(lead_id)
    if lead is None:
        return HTMLResponse("<h1>404 — lead not found</h1>", status_code=404)

    business = lead.business
    context = {
        "page": "leads",
        "lead": lead,
        "business": business,
        "audit": business.audits[0] if business.audits else None,
        "audit_history": business.audits[:10],
        "insight": business.insights[0] if business.insights else None,
        "contacts": business.contacts,
        "activities": lead.activities,
        "lead_statuses": list(LeadStatus),
    }
    return templates.TemplateResponse(request, "lead_detail.html", context)


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request, session: DbSession) -> Any:
    context = {
        "page": "runs",
        "runs": await RunRepository(session).recent(limit=40),
        "reports": await ReportRepository(session).recent(limit=20),
        "locations": get_locations().targets,
        "niches": get_niches().niches,
    }
    return templates.TemplateResponse(request, "runs.html", context)
