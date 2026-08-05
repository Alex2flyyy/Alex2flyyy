"""Daily report generation.

Produces the artifact you actually work from each morning: the top N prospects
with everything needed to make the call, in three formats (HTML to read, CSV to
import, XLSX to hand to someone else).

The row shape defined by :func:`lead_to_row` is the canonical export record.
Every exporter — CSV, Excel, Sheets, Notion, Airtable, CRM — consumes it, so a
new field is added in one place and appears everywhere.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from leadgen.compliance.policy import filter_suppressed
from leadgen.config import get_settings
from leadgen.db.repositories import (
    AnalyticsRepository,
    LeadRepository,
    ReportRepository,
    SuppressionRepository,
)
from leadgen.db.session import session_scope
from leadgen.domain import WebsiteStatus
from leadgen.enrichment.normalize import format_phone_display
from leadgen.logging import get_logger

log = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Column order for CSV/Excel. Deliberate: the fields you need while dialing
# come first, analysis fields after.
EXPORT_COLUMNS = [
    "rank",
    "lead_score",
    "business_name",
    "owner_name",
    "phone",
    "email",
    "website",
    "website_status",
    "website_score",
    "why_they_need_a_website",
    "street_address",
    "city",
    "state",
    "zip_code",
    "county",
    "niche",
    "category",
    "google_rating",
    "review_count",
    "google_maps_url",
    "hours",
    "social_links",
    "business_summary",
    "top_problems",
    "redesign_recommendations",
    "outreach_angle",
    "outreach_subject",
    "talking_points",
    "estimated_value_tier",
    "lead_status",
    "contacted_at",
    "follow_up_at",
    "notes",
    "first_discovered",
    "last_checked",
    "lead_id",
    "business_id",
]


def lead_to_row(lead: Any, *, rank: int | None = None) -> dict[str, Any]:
    """Flatten a Lead + Business + latest audit + latest insight into one row."""
    business = lead.business
    insight = business.insights[0] if getattr(business, "insights", None) else None
    audit = business.audits[0] if getattr(business, "audits", None) else None

    contacts = getattr(business, "contacts", None) or []
    email = business.email or next(
        (c.value for c in contacts if c.kind == "email" and c.is_primary),
        next((c.value for c in contacts if c.kind == "email"), None),
    )
    owner = business.owner_name or next(
        (c.person_name for c in contacts if c.kind == "email" and c.person_name), None
    )

    status: WebsiteStatus = lead.website_status
    hours = business.hours or {}
    socials = business.social_links or {}

    return {
        "rank": rank,
        "lead_score": lead.score,
        "business_name": business.name,
        "owner_name": owner,
        "phone": format_phone_display(business.phone_e164) or business.phone,
        "email": email,
        "website": business.website_url or "",
        "website_status": status.display if hasattr(status, "display") else str(status),
        "website_score": round(lead.website_score, 1) if lead.website_score is not None else "",
        "why_they_need_a_website": lead.reason or "",
        "street_address": business.street_address or "",
        "city": business.city or "",
        "state": business.state or "",
        "zip_code": business.zip_code or "",
        "county": business.county or "",
        "niche": business.niche_key or "",
        "category": ", ".join((business.categories or [])[:3]),
        "google_rating": business.rating if business.rating is not None else "",
        "review_count": business.review_count if business.review_count is not None else "",
        "google_maps_url": business.google_maps_url or "",
        "hours": "; ".join(f"{k}: {v}" for k, v in list(hours.items())[:7]),
        "social_links": "; ".join(f"{k}: {v}" for k, v in socials.items()),
        "business_summary": (insight.summary if insight else "") or business.description or "",
        "top_problems": " | ".join((audit.problems or [])[:5]) if audit else "",
        "redesign_recommendations": " | ".join(insight.redesign_recommendations or [])
        if insight
        else "",
        "outreach_angle": insight.outreach_angle if insight else "",
        "outreach_subject": insight.outreach_subject if insight else "",
        "talking_points": " | ".join(insight.talking_points or []) if insight else "",
        "estimated_value_tier": insight.estimated_value_tier if insight else "",
        "lead_status": lead.status.value if hasattr(lead.status, "value") else str(lead.status),
        "contacted_at": lead.contacted_at.isoformat() if lead.contacted_at else "",
        "follow_up_at": lead.follow_up_at.isoformat() if lead.follow_up_at else "",
        "notes": lead.notes or "",
        "first_discovered": business.first_discovered_at.date().isoformat()
        if business.first_discovered_at
        else "",
        "last_checked": business.last_checked_at.date().isoformat()
        if business.last_checked_at
        else "",
        "lead_id": lead.id,
        "business_id": business.id,
    }


async def collect_report_rows(
    report_date: date | None = None, limit: int | None = None
) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any]]:
    """Gather today's top prospects, suppression-filtered, with stats."""
    settings = get_settings()
    report_date = report_date or date.today()
    limit = limit or settings.daily_lead_target

    async with session_scope() as session:
        lead_repo = LeadRepository(session)
        leads = list(await lead_repo.top_for_date(report_date, limit=limit))

        # Re-fetch with relations loaded; top_for_date only eager-loads the
        # business, and the row builder needs audits, insights, and contacts.
        full_leads = []
        for lead in leads:
            full = await lead_repo.get_full(lead.id)
            if full is not None:
                full_leads.append(full)

        suppressions = await SuppressionRepository(session).active_values()
        analytics = AnalyticsRepository(session)
        stats = {
            "overview": await analytics.overview(report_date),
            "by_city": await analytics.by_city(limit=10),
            "by_niche": await analytics.by_niche(limit=12),
            "by_website_status": await analytics.by_website_status(),
        }

    rows = [lead_to_row(lead, rank=i + 1) for i, lead in enumerate(full_leads)]
    rows = filter_suppressed(rows, suppressions)
    for index, row in enumerate(rows):
        row["rank"] = index + 1

    return rows, full_leads, stats


def render_html(rows: list[dict[str, Any]], stats: dict[str, Any], report_date: date) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("daily_report.html")
    return template.render(
        rows=rows,
        stats=stats,
        report_date=report_date,
        generated_at=datetime.utcnow(),
        lead_count=len(rows),
        avg_score=round(sum(r["lead_score"] for r in rows) / len(rows), 1) if rows else 0,
        no_website_count=sum(1 for r in rows if r["website_status"] == "No Website"),
    )


async def generate_daily_report(
    report_date: date | None = None,
    *,
    limit: int | None = None,
    run_id: int | None = None,
    formats: tuple[str, ...] = ("html", "csv", "xlsx"),
) -> dict[str, Any]:
    """Build and persist the daily report. Returns paths and summary stats."""
    from leadgen.exports.csv_export import write_csv
    from leadgen.exports.excel import write_excel

    settings = get_settings()
    report_date = report_date or date.today()
    rows, leads, stats = await collect_report_rows(report_date, limit)

    out_dir = Path(settings.export_dir) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_date.isoformat()

    paths: dict[str, str] = {}

    if "html" in formats:
        html_path = out_dir / f"leads_{stamp}.html"
        html_path.write_text(render_html(rows, stats, report_date), encoding="utf-8")
        paths["html"] = str(html_path)

    if "csv" in formats:
        paths["csv"] = str(write_csv(rows, out_dir / f"leads_{stamp}.csv"))

    if "xlsx" in formats:
        paths["xlsx"] = str(write_excel(rows, out_dir / f"leads_{stamp}.xlsx", stats=stats))

    summary = _summary_line(rows, stats)

    async with session_scope() as session:
        await ReportRepository(session).upsert(
            report_date,
            {
                "run_id": run_id,
                "lead_ids": [lead.id for lead in leads],
                "lead_count": len(rows),
                "avg_score": round(sum(r["lead_score"] for r in rows) / len(rows), 1)
                if rows
                else None,
                "summary": summary,
                "stats": stats,
                "html_path": paths.get("html"),
                "csv_path": paths.get("csv"),
                "excel_path": paths.get("xlsx"),
            },
        )

    log.info("report.generated", date=stamp, leads=len(rows), formats=list(paths))
    return {
        "date": stamp,
        "lead_count": len(rows),
        "paths": paths,
        "summary": summary,
        "stats": stats,
    }


def _summary_line(rows: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    if not rows:
        return "No qualified leads generated today."
    no_site = sum(1 for r in rows if r["website_status"] == "No Website")
    broken = sum(1 for r in rows if r["website_status"] in {"Broken Website", "Outdated Website"})
    top_city = stats["by_city"][0]["city"] if stats.get("by_city") else "n/a"
    avg = round(sum(r["lead_score"] for r in rows) / len(rows), 1)
    return (
        f"{len(rows)} prospects, average score {avg}. "
        f"{no_site} have no website at all and {broken} have a broken or outdated one. "
        f"Highest-volume city: {top_city}."
    )
