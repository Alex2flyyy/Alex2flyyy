"""Pipeline run endpoints.

The trigger endpoint launches the pipeline as a background task and returns
immediately. A run takes minutes to hours; holding an HTTP connection open for
that would time out at every proxy between the client and the server.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from leadgen.api.deps import DbSession, RequireApiKey
from leadgen.api.schemas import RunOut, RunRequest
from leadgen.db.repositories import ReportRepository, RunRepository
from leadgen.logging import get_logger
from leadgen.pipeline.daily import PipelineConfig, run_daily

log = get_logger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Guards against a double-click or a retrying scheduler starting two
# concurrent pipelines against the same database.
_running = False


@router.get("", response_model=list[RunOut], summary="Recent pipeline runs")
async def list_runs(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[RunOut]:
    runs = await RunRepository(session).recent(limit=limit)
    return [_run_out(run) for run in runs]


@router.get("/latest", response_model=RunOut, summary="Most recent run for a date")
async def latest_run(session: DbSession, day: date | None = None) -> RunOut:
    run = await RunRepository(session).latest_for_date(day or date.today())
    if run is None:
        raise HTTPException(status_code=404, detail="no run found for that date")
    return _run_out(run)


@router.post(
    "/trigger",
    status_code=202,
    dependencies=[RequireApiKey],
    summary="Start a pipeline run in the background",
)
async def trigger_run(payload: RunRequest, background: BackgroundTasks) -> dict[str, Any]:
    global _running
    if _running:
        raise HTTPException(status_code=409, detail="a pipeline run is already in progress")

    config = PipelineConfig(
        location_key=payload.location,
        niche_keys=payload.niches,
        target_leads=payload.target_leads,
        trigger="api",
        use_ai=payload.use_ai,
        use_browser=payload.use_browser,
        use_pagespeed=payload.use_pagespeed,
        discovery_cap=payload.discovery_cap,
        dry_run=payload.dry_run,
    )
    background.add_task(_execute, config)
    return {
        "accepted": True,
        "location": payload.location,
        "niches": payload.niches or "all",
        "message": "run started; poll GET /api/runs/latest for progress",
    }


async def _execute(config: PipelineConfig) -> None:
    global _running
    _running = True
    try:
        result = await run_daily(config)
        if result.status.value in {"completed", "partial"} and not config.dry_run:
            from leadgen.reports.daily_report import generate_daily_report

            await generate_daily_report(run_id=result.run_id)
    except Exception as exc:  # noqa: BLE001 - background task, nothing to bubble to
        log.exception("run.background_failed", error=str(exc))
    finally:
        _running = False


@router.get("/reports", summary="Recent daily reports")
async def list_reports(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 30
) -> list[dict[str, Any]]:
    reports = await ReportRepository(session).recent(limit=limit)
    return [
        {
            "report_date": report.report_date.isoformat(),
            "lead_count": report.lead_count,
            "avg_score": report.avg_score,
            "summary": report.summary,
            "html_path": report.html_path,
            "csv_path": report.csv_path,
            "excel_path": report.excel_path,
        }
        for report in reports
    ]


def _run_out(run: Any) -> RunOut:
    return RunOut(
        id=run.id,
        run_date=run.run_date,
        trigger=run.trigger,
        status=run.status.value,
        location_key=run.location_key,
        niche_keys=list(run.niche_keys or []),
        discovered=run.discovered,
        new_businesses=run.new_businesses,
        duplicates=run.duplicates,
        audited=run.audited,
        scored=run.scored,
        qualified=run.qualified,
        enriched=run.enriched,
        errors=list(run.errors or []),
        stages=list(run.stages or []),
        api_calls=run.api_calls or {},
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=run.duration_seconds,
    )
