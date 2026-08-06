"""Export endpoints."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from leadgen.api.deps import DbSession, RequireApiKey
from leadgen.api.schemas import ExportOut, ExportRequest
from leadgen.compliance.policy import filter_suppressed
from leadgen.config import get_settings
from leadgen.db.repositories import (
    ExportJobRepository,
    LeadRepository,
    ReportRepository,
    SuppressionRepository,
)
from leadgen.exports import (
    DESTINATIONS,
    ExportUnavailable,
    export_to_airtable,
    export_to_crm,
    export_to_google_sheets,
    export_to_notion,
    write_csv,
    write_excel,
)
from leadgen.logging import get_logger
from leadgen.reports.daily_report import lead_to_row

log = get_logger(__name__)

router = APIRouter(prefix="/api/exports", tags=["exports"])


@router.get("/destinations", summary="Available export destinations")
async def destinations() -> dict[str, Any]:
    from leadgen.exports.integrations import CRM_MAPPINGS

    settings = get_settings()
    return {
        "destinations": list(DESTINATIONS),
        "crm_formats": sorted(CRM_MAPPINGS),
        "configured": {
            "google_sheets": bool(
                settings.google_sheets_spreadsheet_id and settings.google_sheets_credentials_file
            ),
            "notion": bool(settings.notion_api_key and settings.notion_database_id),
            "airtable": bool(settings.airtable_api_key and settings.airtable_base_id),
        },
    }


@router.post("", response_model=ExportOut, dependencies=[RequireApiKey], summary="Run an export")
async def create_export(payload: ExportRequest, session: DbSession) -> ExportOut:
    if payload.destination not in DESTINATIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown destination {payload.destination!r}; "
            f"choose from {', '.join(DESTINATIONS)}",
        )

    rows = await _collect_rows(session, payload)
    job_repo = ExportJobRepository(session)
    job = await job_repo.create(payload.destination, payload.filters.model_dump(mode="json"))

    settings = get_settings()
    out_dir = Path(settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    output_path: str | None = None
    remote_ref: str | None = None

    try:
        if payload.destination == "csv":
            output_path = str(write_csv(rows, out_dir / f"leads_{stamp}.csv"))
        elif payload.destination == "xlsx":
            output_path = str(write_excel(rows, out_dir / f"leads_{stamp}.xlsx"))
        elif payload.destination == "crm":
            output_path = str(
                export_to_crm(rows, payload.crm, out_dir / f"leads_{payload.crm}_{stamp}.csv")
            )
        elif payload.destination == "google_sheets":
            remote_ref = export_to_google_sheets(rows)
        elif payload.destination == "notion":
            result = await export_to_notion(rows)
            remote_ref = f"notion:{result['database_id']} ({result['created']} created)"
        elif payload.destination == "airtable":
            result = await export_to_airtable(rows)
            remote_ref = f"airtable:{result['base_id']}/{result['table']} ({result['created']})"

    except ExportUnavailable as exc:
        await job_repo.finish(job.id, status="failed", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await job_repo.finish(job.id, status="failed", error=str(exc)[:500])
        log.exception("export.failed", destination=payload.destination)
        raise HTTPException(status_code=500, detail=f"export failed: {exc}") from exc

    await job_repo.finish(
        job.id,
        status="completed",
        row_count=len(rows),
        output_path=output_path,
        remote_ref=remote_ref,
    )
    return ExportOut(
        destination=payload.destination,
        status="completed",
        row_count=len(rows),
        output_path=output_path,
        remote_ref=remote_ref,
    )


@router.get("/history", summary="Recent export jobs")
async def history(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[dict[str, Any]]:
    jobs = await ExportJobRepository(session).recent(limit=limit)
    return [
        {
            "id": job.id,
            "destination": job.destination,
            "status": job.status,
            "row_count": job.row_count,
            "output_path": job.output_path,
            "remote_ref": job.remote_ref,
            "error": job.error,
            "created_at": job.created_at.isoformat(),
        }
        for job in jobs
    ]


@router.get("/report/{report_date}", summary="Download a generated daily report")
async def download_report(report_date: date, session: DbSession, fmt: str = "html") -> FileResponse:
    report = await ReportRepository(session).get(report_date)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no report for {report_date}")

    path_by_format = {
        "html": report.html_path,
        "csv": report.csv_path,
        "xlsx": report.excel_path,
    }
    path = path_by_format.get(fmt)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"no {fmt} file for {report_date}")

    media = {
        "html": "text/html",
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[fmt]
    return FileResponse(path, media_type=media, filename=Path(path).name)


async def _collect_rows(session: Any, payload: ExportRequest) -> list[dict[str, Any]]:
    repo = LeadRepository(session)
    filters = payload.filters.model_dump(exclude_none=True)
    limit = filters.pop("limit", 500)
    offset = filters.pop("offset", 0)
    order_by = filters.pop("order_by", "score")

    leads = await repo.search(**filters, order_by=order_by, limit=limit, offset=offset)

    rows = []
    for index, lead in enumerate(leads):
        full = await repo.get_full(lead.id)
        if full is not None:
            rows.append(lead_to_row(full, rank=index + 1))

    suppressions = await SuppressionRepository(session).active_values()
    return filter_suppressed(rows, suppressions)
