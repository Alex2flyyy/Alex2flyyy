"""Command line interface.

Every operation the system can perform is reachable from here, so a cron entry,
a Docker CMD, and a human debugging at 6am all use the same code path as the
API. ``leadgen --help`` is the entry point.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from leadgen import __version__
from leadgen.config import get_locations, get_niches, get_scoring, get_settings
from leadgen.logging import configure_logging, get_logger

app = typer.Typer(
    name="leadgen",
    help="Automated lead generation for website design services.",
    no_args_is_help=True,
    add_completion=False,
)
geo_app = typer.Typer(help="Gazetteer and geography tools.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
app.add_typer(geo_app, name="geo")
app.add_typer(config_app, name="config")

console = Console()
log = get_logger(__name__)


@app.callback()
def _setup(
    log_level: Annotated[str, typer.Option(help="DEBUG, INFO, WARNING, ERROR")] = "",
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Emit JSON log lines")] = False,
) -> None:
    settings = get_settings()
    configure_logging(
        log_level or settings.log_level,
        "json" if json_logs else settings.log_format,
    )


# =============================================================================
# Pipeline
# =============================================================================


@app.command("run-daily")
def run_daily_cmd(
    location: Annotated[str, typer.Option("--location", "-l", help="Location target key")] = "",
    niche: Annotated[list[str], typer.Option("--niche", "-n", help="Niche key (repeatable)")] = [],
    target: Annotated[int, typer.Option("--target", "-t", help="Qualified leads wanted")] = 0,
    cap: Annotated[int, typer.Option("--cap", help="Max businesses to discover")] = 0,
    no_ai: Annotated[bool, typer.Option("--no-ai", help="Skip AI enrichment")] = False,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="HTTP-only audits")] = False,
    no_pagespeed: Annotated[bool, typer.Option("--no-pagespeed", help="Skip PageSpeed")] = False,
    no_report: Annotated[bool, typer.Option("--no-report", help="Skip report generation")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Discover but write nothing")] = False,
) -> None:
    """Run the full daily pipeline: discover, audit, score, report."""
    from leadgen.pipeline.daily import PipelineConfig, run_daily

    settings = get_settings()
    config = PipelineConfig(
        location_key=location or "home_base_25mi",
        niche_keys=list(niche),
        target_leads=target or settings.daily_lead_target,
        trigger="manual",
        discovery_cap=cap or None,
        use_ai=not no_ai,
        use_browser=False if no_browser else None,
        use_pagespeed=not no_pagespeed,
        dry_run=dry_run,
    )

    async def go() -> Any:
        result = await run_daily(config)
        report = None
        if not dry_run and not no_report and result.status.value in {"completed", "partial"}:
            from leadgen.reports.daily_report import generate_daily_report

            report = await generate_daily_report(run_id=result.run_id)
        return result, report

    result, report = asyncio.run(go())
    _print_run_result(result, report)
    raise typer.Exit(0 if result.status.value in {"completed", "partial"} else 1)


@app.command()
def scheduler(
    hour: Annotated[int, typer.Option(help="Local hour to run, 0-23")] = -1,
    location: Annotated[str, typer.Option("--location", "-l")] = "",
    niche: Annotated[list[str], typer.Option("--niche", "-n")] = [],
) -> None:
    """Run forever, executing the pipeline once a day at the configured hour.

    Use this when you want scheduling inside the container. If you already have
    cron, systemd timers, or GitHub Actions, prefer those and skip this — an
    external scheduler survives a container restart mid-wait.
    """
    from leadgen.pipeline.daily import PipelineConfig, run_daily

    settings = get_settings()
    target_hour = hour if hour >= 0 else settings.schedule_hour
    console.print(f"[green]Scheduler started.[/] Running daily at {target_hour:02d}:00 local time.")

    async def loop() -> None:
        last_run: date | None = None
        while True:
            now = datetime.now()
            if now.hour == target_hour and last_run != now.date():
                console.print(f"[cyan]Starting scheduled run for {now.date()}[/]")
                try:
                    result = await run_daily(
                        PipelineConfig(
                            location_key=location or "home_base_25mi",
                            niche_keys=list(niche),
                            target_leads=settings.daily_lead_target,
                            trigger="scheduled",
                        )
                    )
                    if result.status.value in {"completed", "partial"}:
                        from leadgen.reports.daily_report import generate_daily_report

                        await generate_daily_report(run_id=result.run_id)
                    console.print(f"[green]Run finished: {result.status.value}[/]")
                except Exception as exc:
                    log.exception("scheduler.run_failed", error=str(exc))
                # Mark the date regardless of outcome so a failing run does not
                # retry in a tight loop for the rest of the hour.
                last_run = now.date()
            await asyncio.sleep(300)

    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped.[/]")


# =============================================================================
# Reports and exports
# =============================================================================


@app.command()
def report(
    day: Annotated[str, typer.Option("--date", help="YYYY-MM-DD, defaults to today")] = "",
    limit: Annotated[int, typer.Option("--limit", help="How many leads")] = 0,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the HTML report")] = False,
) -> None:
    """Generate today's report (HTML + CSV + Excel) from existing leads."""
    from leadgen.reports.daily_report import generate_daily_report

    report_date = date.fromisoformat(day) if day else date.today()
    result = asyncio.run(generate_daily_report(report_date, limit=limit or None))

    console.print(
        Panel(
            f"[bold]{result['lead_count']} leads[/]\n\n{result['summary']}\n\n"
            + "\n".join(f"{fmt.upper():5} {path}" for fmt, path in result["paths"].items()),
            title=f"Report — {result['date']}",
            border_style="green",
        )
    )

    if open_browser and (html := result["paths"].get("html")):
        import webbrowser

        webbrowser.open(f"file://{Path(html).resolve()}")


@app.command("export")
def export_cmd(
    destination: Annotated[
        str, typer.Argument(help="csv | xlsx | google_sheets | notion | airtable | crm")
    ],
    crm: Annotated[str, typer.Option(help="CRM format when destination=crm")] = "generic",
    limit: Annotated[int, typer.Option(help="Max leads")] = 500,
    min_score: Annotated[int, typer.Option(help="Minimum lead score")] = 0,
    niche: Annotated[list[str], typer.Option("--niche", "-n")] = [],
    city: Annotated[list[str], typer.Option("--city", "-c")] = [],
    qualified_only: Annotated[bool, typer.Option("--qualified-only/--all")] = True,
    out: Annotated[str, typer.Option("--out", "-o", help="Output path for file exports")] = "",
) -> None:
    """Export leads to a file or an external service."""
    from leadgen.compliance.policy import filter_suppressed
    from leadgen.db.repositories import LeadRepository, SuppressionRepository
    from leadgen.db.session import session_scope
    from leadgen.exports import (
        ExportUnavailable,
        export_to_airtable,
        export_to_crm,
        export_to_google_sheets,
        export_to_notion,
        write_csv,
        write_excel,
    )
    from leadgen.reports.daily_report import lead_to_row

    async def collect() -> list[dict[str, Any]]:
        async with session_scope() as session:
            repo = LeadRepository(session)
            leads = await repo.search(
                min_score=min_score or None,
                niches=list(niche) or None,
                cities=list(city) or None,
                qualified_only=qualified_only,
                order_by="score",
                limit=limit,
            )
            rows = []
            for index, lead in enumerate(leads):
                full = await repo.get_full(lead.id)
                if full is not None:
                    rows.append(lead_to_row(full, rank=index + 1))
            suppressions = await SuppressionRepository(session).active_values()
        return filter_suppressed(rows, suppressions)

    rows = asyncio.run(collect())
    if not rows:
        console.print("[yellow]No leads matched those filters.[/]")
        raise typer.Exit(0)

    settings = get_settings()
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(settings.export_dir)

    try:
        if destination == "csv":
            path = write_csv(rows, Path(out) if out else out_dir / f"leads_{stamp}.csv")
            console.print(f"[green]Wrote {len(rows)} rows[/] → {path}")
        elif destination == "xlsx":
            path = write_excel(rows, Path(out) if out else out_dir / f"leads_{stamp}.xlsx")
            console.print(f"[green]Wrote {len(rows)} rows[/] → {path}")
        elif destination == "crm":
            path = export_to_crm(
                rows, crm, Path(out) if out else out_dir / f"leads_{crm}_{stamp}.csv"
            )
            console.print(f"[green]Wrote {len(rows)} rows in {crm} format[/] → {path}")
        elif destination == "google_sheets":
            url = export_to_google_sheets(rows)
            console.print(f"[green]Pushed {len(rows)} rows[/] → {url}")
        elif destination == "notion":
            result = asyncio.run(export_to_notion(rows))
            console.print(
                f"[green]Notion: {result['created']} created, {result['failed']} failed[/]"
            )
        elif destination == "airtable":
            result = asyncio.run(export_to_airtable(rows))
            console.print(
                f"[green]Airtable: {result['created']} created, {result['failed']} failed[/]"
            )
        else:
            console.print(f"[red]Unknown destination {destination!r}[/]")
            raise typer.Exit(1)
    except ExportUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


# =============================================================================
# Database
# =============================================================================


@app.command("init-db")
def init_db(
    drop: Annotated[bool, typer.Option("--drop", help="DROP every table first")] = False,
) -> None:
    """Create tables directly from the models.

    Convenient for a throwaway dev database. For anything you care about, use
    ``alembic upgrade head`` so schema changes are versioned and reversible.
    """
    from leadgen.db.base import Base
    from leadgen.db.session import dispose_engine, get_engine

    if drop and not typer.confirm("This deletes ALL data. Continue?"):
        raise typer.Abort()

    async def go() -> None:
        engine = get_engine()
        async with engine.begin() as conn:
            if drop:
                await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await dispose_engine()

    asyncio.run(go())
    console.print("[green]Database schema created.[/]")


@geo_app.command("import")
def geo_import(
    path: Annotated[str, typer.Argument(help="CSV of US places")],
    name_col: str = "city",
    state_col: str = "state_id",
    county_col: str = "county_name",
    lat_col: str = "lat",
    lng_col: str = "lng",
    population_col: str = "population",
    zips_col: str = "zips",
    min_population: int = 0,
) -> None:
    """Load a gazetteer of US places.

    This is what makes state-wide and nationwide targets work: cells are seeded
    from real population centers instead of blind-tiling empty land.

    Column defaults match the free SimpleMaps "US Cities" basic CSV. The Census
    Gazetteer files work too with different column names.
    """
    from leadgen.db.repositories import GeoRepository
    from leadgen.db.session import session_scope
    from leadgen.enrichment.normalize import normalize_name

    source = Path(path)
    if not source.exists():
        console.print(f"[red]File not found: {source}[/]")
        raise typer.Exit(1)

    rows: list[dict[str, Any]] = []
    skipped = 0
    with source.open(newline="", encoding="utf-8-sig") as fh:
        for record in csv.DictReader(fh):
            try:
                population = int(float(record.get(population_col) or 0))
                if population < min_population:
                    skipped += 1
                    continue
                name = (record.get(name_col) or "").strip()
                state = (record.get(state_col) or "").strip().upper()[:2]
                if not name or not state:
                    skipped += 1
                    continue
                rows.append(
                    {
                        "name": name[:160],
                        "normalized_name": normalize_name(name)[:160],
                        "state": state,
                        "county": (record.get(county_col) or "").strip()[:120] or None,
                        "lat": float(record[lat_col]),
                        "lng": float(record[lng_col]),
                        "population": population,
                        "zips": (record.get(zips_col) or "").split()[:60],
                        "country_code": "US",
                    }
                )
            except (KeyError, TypeError, ValueError):
                skipped += 1

    if not rows:
        console.print("[red]No usable rows. Check the column names against your CSV header.[/]")
        raise typer.Exit(1)

    async def go() -> int:
        total = 0
        async with session_scope() as session:
            repo = GeoRepository(session)
            for start in range(0, len(rows), 1000):
                total += await repo.bulk_upsert(rows[start : start + 1000])
            return await repo.count()

    stored = asyncio.run(go())
    console.print(
        f"[green]Imported {len(rows)} places[/] (skipped {skipped}). "
        f"Gazetteer now holds {stored} places."
    )


@geo_app.command("seed")
def geo_seed() -> None:
    """Load the bundled starter gazetteer (~250 US places, CA-weighted).

    Enough to make city, county, and radius targets work immediately. For
    genuine statewide or nationwide coverage, import a full gazetteer with
    ``leadgen geo import`` — see docs/INSTALL.md.
    """
    seed_file = get_settings().config_dir / "seed_places.csv"
    if not seed_file.exists():
        console.print(f"[red]Bundled seed file missing: {seed_file}[/]")
        raise typer.Exit(1)
    geo_import(str(seed_file))
    console.print(
        "[yellow]Note:[/] this is a starter set. Nationwide targets need a full "
        "gazetteer — see `leadgen geo import --help`."
    )


@geo_app.command("plan")
def geo_plan(
    location: Annotated[str, typer.Argument(help="Location target key")],
    niche: Annotated[list[str], typer.Option("--niche", "-n")] = [],
) -> None:
    """Show the search cells and projected API cost for a location, without running."""
    from leadgen.db.session import session_scope
    from leadgen.geo.geocoder import Geocoder
    from leadgen.geo.resolver import LocationResolver, estimate_cost
    from leadgen.http import HttpFetcher

    target = get_locations().get(location)
    niches = list(niche) or get_niches().keys

    async def go() -> tuple[list[Any], dict[str, Any]]:
        async with HttpFetcher() as fetcher, session_scope() as session:
            resolver = LocationResolver(Geocoder(fetcher, session), session)
            cells = await resolver.resolve(target)
            return cells, await estimate_cost(cells, len(niches))

    cells, cost = asyncio.run(go())

    table = Table(title=f"Search plan — {target.label}")
    table.add_column("#", justify="right")
    table.add_column("Cell")
    table.add_column("Lat/Lng")
    table.add_column("Radius", justify="right")
    table.add_column("Population", justify="right")
    for index, cell in enumerate(cells[:25], 1):
        table.add_row(
            str(index),
            cell.label[:44],
            f"{cell.lat:.4f}, {cell.lng:.4f}",
            f"{cell.radius_m / 1000:.1f}km",
            f"{cell.population:,}" if cell.population else "—",
        )
    console.print(table)
    if len(cells) > 25:
        console.print(f"[dim]… and {len(cells) - 25} more cells[/]")

    console.print(
        Panel(
            f"Cells: [bold]{cost['cells']}[/]\n"
            f"Niches: [bold]{cost['niches']}[/]\n"
            f"Provider searches: [bold]{cost['searches']:,}[/]\n"
            f"Estimated cost: [bold]${cost['estimated_usd']:,.2f}[/]\n\n"
            f"[dim]{cost['note']}[/]",
            title="Projected cost",
            border_style="yellow",
        )
    )


# =============================================================================
# Diagnostics
# =============================================================================


@app.command()
def audit(
    url: Annotated[str, typer.Argument(help="Website URL to audit")],
    no_browser: Annotated[bool, typer.Option("--no-browser")] = False,
    no_pagespeed: Annotated[bool, typer.Option("--no-pagespeed")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Audit a single website and print the findings. No database required."""
    from leadgen.evaluation.auditor import AuditOptions, WebsiteAuditor
    from leadgen.evaluation.browser import BrowserAuditor, NullBrowserAuditor
    from leadgen.http import HttpFetcher
    from leadgen.scoring.website_score import summarize

    async def go() -> Any:
        browser = NullBrowserAuditor() if no_browser else BrowserAuditor()
        async with HttpFetcher() as fetcher:
            await browser.start()
            try:
                auditor = WebsiteAuditor(
                    fetcher,
                    browser=browser,
                    options=AuditOptions(
                        use_browser=not no_browser,
                        use_pagespeed=not no_pagespeed,
                        take_screenshots=False,
                    ),
                )
                return await auditor.audit(url)
            finally:
                await browser.stop()

    result = asyncio.run(go())

    if as_json:
        from dataclasses import asdict

        console.print_json(json.dumps(asdict(result), default=str))
        return

    console.print(
        Panel(
            f"[bold]{result.score:.0f}/100[/]  —  {result.status.display}\n\n{summarize(result)}",
            title=result.url,
            border_style="red" if result.score < 50 else "yellow" if result.score < 70 else "green",
        )
    )

    if result.dimension_scores:
        table = Table(title="Dimension scores")
        table.add_column("Dimension")
        table.add_column("Score", justify="right")
        for name, value in sorted(result.dimension_scores.items(), key=lambda kv: kv[1]):
            color = "red" if value < 40 else "yellow" if value < 70 else "green"
            table.add_row(name.replace("_", " ").title(), f"[{color}]{value:.0f}[/]")
        console.print(table)

    if result.problems:
        console.print("\n[bold]Problems found[/]")
        for problem in result.problems:
            console.print(f"  • {problem}")

    facts = [
        ("Outcome", result.outcome.value),
        ("Final URL", result.final_url or "—"),
        ("HTTPS", "yes" if result.uses_https else "NO"),
        ("SSL valid", "yes" if result.ssl_valid else "no"),
        ("Mobile friendly", str(result.mobile_friendly)),
        ("Design era", result.design_era or "—"),
        ("Built with", ", ".join(result.tech_stack) or "—"),
        ("PageSpeed", f"{result.psi_performance:.0f}" if result.psi_performance else "—"),
        ("Contact form", "yes" if result.has_contact_form else "no"),
        ("Emails found", ", ".join(result.emails) or "—"),
        ("Audit time", f"{result.duration_ms}ms"),
    ]
    console.print("\n[bold]Facts[/]")
    for label, value in facts:
        console.print(f"  {label:<16} [dim]{value}[/]")


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "",
    port: Annotated[int, typer.Option()] = 0,
    reload: Annotated[bool, typer.Option("--reload")] = False,
) -> None:
    """Start the API and dashboard."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "leadgen.api.app:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
        log_config=None,
    )


@app.command()
def suppress(
    value: Annotated[str, typer.Argument(help="Email, phone, domain, or business name")],
    kind: Annotated[str, typer.Option(help="email | phone | domain | business")] = "email",
    reason: Annotated[str, typer.Option()] = "opt-out requested",
) -> None:
    """Add an entry to the do-not-contact list."""
    from leadgen.db.repositories import SuppressionRepository
    from leadgen.db.session import session_scope

    async def go() -> None:
        async with session_scope() as session:
            await SuppressionRepository(session).add(kind, value.lower(), reason)

    asyncio.run(go())
    console.print(f"[green]Suppressed[/] {kind}: {value}")


@app.command()
def stats() -> None:
    """Print current pipeline statistics."""
    from leadgen.db.repositories import AnalyticsRepository
    from leadgen.db.session import session_scope

    async def go() -> dict[str, Any]:
        async with session_scope() as session:
            repo = AnalyticsRepository(session)
            return {
                "overview": await repo.overview(),
                "by_niche": await repo.by_niche(limit=10),
                "by_city": await repo.by_city(limit=10),
                "funnel": await repo.conversion_funnel(),
            }

    data = asyncio.run(go())
    overview = data["overview"]

    table = Table(title="Overview")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for key, label in [
        ("total_businesses", "Businesses"),
        ("total_leads", "Leads"),
        ("qualified_leads", "Qualified"),
        ("new_today", "New today"),
        ("contacted", "Contacted"),
        ("pending", "Pending"),
        ("won", "Won"),
        ("avg_score", "Avg score"),
        ("contact_rate", "Contact rate %"),
        ("win_rate", "Win rate %"),
    ]:
        table.add_row(label, str(overview.get(key, 0)))
    console.print(table)

    funnel = Table(title="Funnel")
    funnel.add_column("Stage")
    funnel.add_column("Count", justify="right")
    for stage in data["funnel"]:
        funnel.add_row(stage["stage"], str(stage["count"]))
    console.print(funnel)


# =============================================================================
# Config inspection
# =============================================================================


@config_app.command("show")
def config_show() -> None:
    """Print effective settings. Secrets are masked."""
    settings = get_settings()
    table = Table(title=f"leadgen {__version__} — {settings.env}")
    table.add_column("Setting")
    table.add_column("Value")

    for field in sorted(settings.model_fields):
        value = getattr(settings, field)
        if any(s in field for s in ("key", "token", "password", "secret", "url")) and value:
            text = str(value)
            value = f"{text[:6]}…{text[-4:]}" if len(text) > 14 else "***"
        table.add_row(field, str(value))
    console.print(table)


@config_app.command("niches")
def config_niches() -> None:
    """List configured niches."""
    table = Table(title="Niches")
    for column in ("Key", "Label", "Demand", "Competition", "Ticket", "Place types"):
        table.add_column(column)
    for niche in get_niches().niches:
        table.add_row(
            niche.key,
            niche.label,
            f"{niche.demand:.2f}",
            f"{niche.competition:.2f}",
            f"{niche.ticket:.2f}",
            ", ".join(niche.place_types[:3]) or "—",
        )
    console.print(table)


@config_app.command("locations")
def config_locations() -> None:
    """List configured location targets."""
    table = Table(title="Location targets")
    for column in ("Key", "Label", "Kind", "Cell radius", "Max cells"):
        table.add_column(column)
    for key, target in sorted(get_locations().targets.items()):
        table.add_row(
            key,
            target.label,
            target.kind,
            f"{target.cell_radius_m / 1000:.1f}km",
            str(target.max_cells),
        )
    console.print(table)


@config_app.command("check")
def config_check() -> None:
    """Validate configuration and report what is and isn't wired up."""
    settings = get_settings()
    problems: list[str] = []
    warnings: list[str] = []

    try:
        niches = get_niches()
        locations = get_locations()
        get_scoring()
        console.print(
            f"[green]✓[/] Config files valid ({len(niches.niches)} niches, "
            f"{len(locations.targets)} locations)"
        )
    except Exception as exc:
        problems.append(f"config files: {exc}")

    if not settings.google_maps_api_key:
        warnings.append(
            "LEADGEN_GOOGLE_MAPS_API_KEY is not set — discovery falls back to "
            "OpenStreetMap only, and geocoding uses Nominatim"
        )
    else:
        console.print("[green]✓[/] Google Maps API key present")

    if not settings.pagespeed_key:
        warnings.append("No PageSpeed key — speed and Core Web Vitals scoring is disabled")

    if not settings.anthropic_api_key:
        warnings.append("ANTHROPIC_API_KEY is not set — AI enrichment is disabled")
    elif settings.ai_enabled:
        console.print(f"[green]✓[/] AI enrichment enabled ({settings.ai_model})")

    from leadgen.db.session import dispose_engine, get_engine

    async def check_db() -> str:
        from sqlalchemy import text

        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await dispose_engine()
            return "ok"
        except Exception as exc:
            return str(exc)[:200]

    db_status = asyncio.run(check_db())
    if db_status == "ok":
        console.print("[green]✓[/] Database reachable")
    else:
        problems.append(f"database unreachable: {db_status}")

    try:
        import playwright  # noqa: F401

        console.print("[green]✓[/] Playwright installed")
    except ImportError:
        warnings.append(
            "Playwright is not installed — audits will be HTTP-only "
            "(no mobile rendering checks or screenshots). Run: playwright install chromium"
        )

    for warning in warnings:
        console.print(f"[yellow]![/] {warning}")
    for problem in problems:
        console.print(f"[red]✗[/] {problem}")

    if problems:
        raise typer.Exit(1)
    console.print("\n[green]Configuration is usable.[/]")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"leadgen {__version__}")


# =============================================================================


def _print_run_result(result: Any, report: dict[str, Any] | None) -> None:
    color = {"completed": "green", "partial": "yellow", "failed": "red"}.get(
        result.status.value, "white"
    )
    table = Table(title=f"Run {result.run_id or '(dry run)'} — {result.status.value}")
    table.add_column("Stage")
    table.add_column("Processed", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Skipped", justify="right")
    table.add_column("Seconds", justify="right")
    for stage in result.stages:
        table.add_row(
            stage.name,
            str(stage.processed),
            str(stage.succeeded),
            str(stage.failed),
            str(stage.skipped),
            f"{stage.duration_s:.1f}",
        )
    console.print(table)

    summary = (
        f"Discovered [bold]{result.discovered}[/] · "
        f"New [bold]{result.new_businesses}[/] · "
        f"Duplicates [bold]{result.duplicates}[/]\n"
        f"Audited [bold]{result.audited}[/] · "
        f"Scored [bold]{result.scored}[/] · "
        f"Qualified [bold]{result.qualified}[/] · "
        f"AI-enriched [bold]{result.enriched}[/]\n"
        f"Duration [bold]{result.duration_s:.0f}s[/]"
    )
    if report:
        summary += f"\n\nReport: {report['paths'].get('html', '—')}"
    if result.errors:
        summary += f"\n\n[red]{len(result.errors)} errors[/] (first: {result.errors[0][:160]})"

    console.print(Panel(summary, title="Result", border_style=color))


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
