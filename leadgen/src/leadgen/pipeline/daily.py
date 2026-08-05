"""The daily pipeline.

One command, one run, one report. Stages, in order:

1. **Plan** — resolve the location target into ordered search cells and pick
   the niches to work.
2. **Discover** — query providers cell by cell, deduplicating in memory.
3. **Persist** — upsert into the database, where unique constraints catch any
   duplicate that survived stage 2.
4. **Audit** — evaluate every website (new businesses first, then the stalest
   existing records, so the database stays fresh without re-auditing
   everything nightly).
5. **Enrich contacts** — find emails and forms on the businesses' own sites.
6. **Score** — compute lead scores and qualification.
7. **AI enrich** — generate summaries and outreach angles for the top N only.
8. **Report** — build today's top 50, write the report, run exports.

The run is designed to degrade rather than fail. Provider quota exhausted?
Continue with what was found. AI budget gone? Ship the report without
summaries. Browser won't launch? Fall back to HTTP-only audits. A partially
successful run that produces 38 leads is worth infinitely more than a clean
exception at 6:04am.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from leadgen.ai.client import AIClient
from leadgen.ai.enrichment import EnrichmentService, compute_input_hash
from leadgen.compliance.robots import RobotsChecker
from leadgen.config import Niche, get_locations, get_niches, get_scoring, get_settings
from leadgen.db.models import Business
from leadgen.db.repositories import (
    AuditRepository,
    BusinessRepository,
    InsightRepository,
    LeadRepository,
    RunRepository,
)
from leadgen.db.session import session_scope
from leadgen.dedupe.matcher import InMemoryDeduper
from leadgen.discovery.registry import DiscoveryOrchestrator, build_providers
from leadgen.domain import (
    AuditOutcome,
    RawBusiness,
    RunResult,
    RunStatus,
    SearchCell,
    StageStats,
    WebsiteAudit,
)
from leadgen.enrichment.contact import ContactEnricher
from leadgen.evaluation.auditor import AuditOptions, WebsiteAuditor
from leadgen.evaluation.browser import BrowserAuditor, NullBrowserAuditor
from leadgen.evaluation.pagespeed import PageSpeedClient
from leadgen.geo.geocoder import Geocoder
from leadgen.geo.resolver import LocationResolver
from leadgen.http import HttpFetcher
from leadgen.logging import bind_context, clear_context, get_logger
from leadgen.pipeline import stages
from leadgen.ratelimit import QuotaCounter
from leadgen.scoring.lead_score import score_lead

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineConfig:
    location_key: str = "home_base_25mi"
    niche_keys: list[str] = field(default_factory=list)
    target_leads: int = 50
    trigger: str = "scheduled"

    discovery_cap: int | None = None
    per_cell_limit: int = 20
    recheck_stale_after_days: int = 21
    recheck_budget: int = 100

    use_browser: bool | None = None
    use_pagespeed: bool = True
    use_ai: bool = True
    ai_enrich_top_n: int = 50
    enrich_contacts: bool = True
    check_broken_links: bool = True

    max_api_calls: int | None = None
    dry_run: bool = False

    def resolved_niches(self) -> list[Niche]:
        catalog = get_niches()
        keys = self.niche_keys or catalog.keys
        return [catalog.get(k) for k in keys]


class DailyPipeline:
    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.settings = get_settings()
        self.config = config or PipelineConfig(
            target_leads=self.settings.daily_lead_target
        )
        self.scoring = get_scoring()
        self.result = RunResult(run_id=None, run_date=date.today(), status=RunStatus.PENDING)
        self.quota = QuotaCounter(
            {"discovery": self.config.max_api_calls} if self.config.max_api_calls else {}
        )
        self._run_id: int | None = None

    # ------------------------------------------------------------------

    async def run(self) -> RunResult:
        started = datetime.utcnow()
        self.result.started_at = started
        self.result.status = RunStatus.RUNNING

        location = get_locations().get(self.config.location_key)
        niches = self.config.resolved_niches()

        await self._open_run(location.key or self.config.location_key, niches)
        bind_context(run_id=self._run_id, location=self.config.location_key)

        log.info(
            "pipeline.start",
            location=location.label,
            niches=len(niches),
            target=self.config.target_leads,
            dry_run=self.config.dry_run,
        )

        fetcher = HttpFetcher()
        browser = self._make_browser()

        try:
            async with fetcher:
                await browser.start()
                try:
                    cells = await self._stage_plan(fetcher, location)
                    if not cells:
                        raise RuntimeError(
                            f"location {self.config.location_key!r} resolved to zero search "
                            "cells — check the gazetteer or the target definition"
                        )

                    discovered = await self._stage_discover(fetcher, cells, niches)
                    business_ids = await self._stage_persist(discovered)
                    audit_targets = await self._stage_select_audit_targets(business_ids)
                    audits = await self._stage_audit(fetcher, browser, audit_targets)
                    await self._stage_score(audits)
                    await self._stage_ai(audits)
                finally:
                    await browser.stop()

            self.result.status = (
                RunStatus.COMPLETED if not self.result.errors else RunStatus.PARTIAL
            )

        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised context-free
            self.result.status = RunStatus.FAILED
            self.result.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("pipeline.failed", error=str(exc))
        finally:
            self.result.finished_at = datetime.utcnow()
            await self._close_run()
            clear_context()

        log.info(
            "pipeline.finished",
            status=self.result.status.value,
            discovered=self.result.discovered,
            new=self.result.new_businesses,
            audited=self.result.audited,
            qualified=self.result.qualified,
            seconds=round(self.result.duration_s, 1),
        )
        return self.result

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    async def _stage_plan(self, fetcher: HttpFetcher, location: Any) -> list[SearchCell]:
        stage = self._begin("plan")
        async with session_scope() as session:
            geocoder = Geocoder(fetcher, session)
            resolver = LocationResolver(geocoder, session)
            cells = await resolver.resolve(location)
        stage.processed = len(cells)
        stage.succeeded = len(cells)
        stage.notes = {"cell_radius_m": location.cell_radius_m, "kind": location.kind}
        self._end(stage)
        return cells

    async def _stage_discover(
        self, fetcher: HttpFetcher, cells: list[SearchCell], niches: list[Niche]
    ) -> list[RawBusiness]:
        stage = self._begin("discover")
        orchestrator = DiscoveryOrchestrator(build_providers(fetcher))
        deduper = InMemoryDeduper()
        cap = self.config.discovery_cap or self.settings.daily_discovery_cap

        collected: list[RawBusiness] = []

        # Cells outer, niches inner: this walks the highest-yield geography
        # first for every niche, so an early stop still gives broad coverage
        # rather than one niche done exhaustively and the rest untouched.
        for cell in cells:
            if len(collected) >= cap:
                log.info("discover.cap_reached", cap=cap)
                break
            for niche in niches:
                if len(collected) >= cap:
                    break
                if self.config.max_api_calls and self.quota.exceeded("discovery"):
                    log.warning("discover.api_budget_reached", calls=self.quota.counts["discovery"])
                    break

                try:
                    found = await orchestrator.search_cell(
                        cell, niche, limit=self.config.per_cell_limit
                    )
                except Exception as exc:  # noqa: BLE001
                    self.result.errors.append(f"discover {cell.label}/{niche.key}: {exc}")
                    stage.failed += 1
                    continue

                # Providers own their own call counters; mirror the running
                # total into the quota so the budget check above sees it.
                self.quota.counts["discovery"] = orchestrator.total_api_calls()
                stage.processed += len(found)

                for business in found:
                    if not business.is_operational:
                        stage.skipped += 1
                        continue
                    if deduper.seen(business):
                        continue
                    collected.append(business)

        stage.succeeded = len(collected)
        stage.notes = {
            "duplicates_in_run": deduper.duplicates_dropped,
            "providers": orchestrator.stats(),
            "cells_searched": min(len(cells), len(cells)),
        }
        self.result.discovered = len(collected)
        self.result.api_calls.update(
            {name: info["api_calls"] for name, info in orchestrator.stats().items()}
        )
        self._end(stage)

        log.info(
            "discover.complete",
            found=len(collected),
            duplicates_dropped=deduper.duplicates_dropped,
            api_calls=orchestrator.total_api_calls(),
        )
        return collected

    async def _stage_persist(self, records: list[RawBusiness]) -> list[int]:
        stage = self._begin("persist")
        if self.config.dry_run:
            log.info("persist.skipped_dry_run", count=len(records))
            stage.skipped = len(records)
            self._end(stage)
            return []

        all_ids: list[int] = []
        created_total = 0
        duplicate_total = 0

        # Batched commits: one transaction for 4,000 upserts would hold locks
        # for the whole stage and lose everything on a single conflict.
        for batch in stages.chunk(records, 100):
            async with session_scope() as session:
                ids, created, duplicates = await stages.persist_businesses(
                    session,
                    batch,
                    location_key=self.config.location_key,
                    scoring_config=self.scoring,
                )
            all_ids.extend(ids)
            created_total += created
            duplicate_total += duplicates

        stage.processed = len(records)
        stage.succeeded = created_total
        stage.notes = {"duplicates": duplicate_total}
        self.result.new_businesses = created_total
        self.result.duplicates = duplicate_total
        self._end(stage)

        log.info("persist.complete", new=created_total, duplicates=duplicate_total)
        return all_ids

    async def _stage_select_audit_targets(self, business_ids: list[int]) -> list[int]:
        """Today's discoveries plus a slice of the stalest existing records.

        Re-auditing everything nightly is wasteful and rude; never re-auditing
        means the database rots and you pitch someone who fixed their site six
        months ago. A rolling slice keeps it fresh at bounded cost.
        """
        if self.config.dry_run:
            return []

        targets = list(business_ids)
        seen = set(targets)
        async with session_scope() as session:
            stale = await BusinessRepository(session).stale_for_recheck(
                self.config.recheck_stale_after_days, self.config.recheck_budget
            )
            for business in stale:
                if business.id not in seen:
                    seen.add(business.id)
                    targets.append(business.id)
        return targets

    async def _stage_audit(
        self,
        fetcher: HttpFetcher,
        browser: BrowserAuditor | NullBrowserAuditor,
        business_ids: list[int],
    ) -> dict[int, WebsiteAudit | None]:
        stage = self._begin("audit")
        audits: dict[int, WebsiteAudit | None] = {}
        if not business_ids:
            self._end(stage)
            return audits

        robots = RobotsChecker(fetcher.client)
        auditor = WebsiteAuditor(
            fetcher,
            browser=browser,
            pagespeed=PageSpeedClient(fetcher),
            robots=robots,
            options=AuditOptions(
                use_browser=not isinstance(browser, NullBrowserAuditor),
                use_pagespeed=self.config.use_pagespeed,
                check_broken_links=self.config.check_broken_links,
                take_screenshots=self.settings.enable_screenshots,
            ),
        )
        contact_enricher = ContactEnricher(fetcher, robots) if self.config.enrich_contacts else None
        semaphore = asyncio.Semaphore(self.settings.max_concurrent_audits)

        async def audit_one(business: Business) -> tuple[int, WebsiteAudit | None, list[dict]]:
            if not business.website_url:
                # No website is a finding, not a failure — it is the single
                # strongest signal in the whole system.
                return business.id, None, []
            async with semaphore:
                try:
                    audit = await auditor.audit(business.website_url)
                except Exception as exc:  # noqa: BLE001
                    log.warning("audit.error", business=business.name, error=str(exc)[:200])
                    return business.id, None, []

                contact_rows: list[dict] = []
                if contact_enricher and audit.outcome == AuditOutcome.OK:
                    try:
                        findings = await contact_enricher.enrich(audit.final_url or audit.url)
                        contact_rows = findings.as_contact_rows()
                        if findings.emails:
                            audit.emails = list(dict.fromkeys(audit.emails + findings.emails))
                        if findings.has_contact_form:
                            audit.has_contact_form = True
                        if findings.social_links:
                            audit.social_links = {**findings.social_links, **audit.social_links}
                    except Exception as exc:  # noqa: BLE001
                        log.debug("contact.enrich_failed", error=str(exc)[:200])
                return business.id, audit, contact_rows

        for batch_ids in stages.chunk(business_ids, 50):
            async with session_scope() as session:
                repo = BusinessRepository(session)
                businesses = [b for b in [await repo.get(i) for i in batch_ids] if b is not None]

            outcomes = await asyncio.gather(
                *(audit_one(b) for b in businesses), return_exceptions=True
            )

            async with session_scope() as session:
                repo = BusinessRepository(session)
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        stage.failed += 1
                        continue
                    business_id, audit, contact_rows = outcome
                    stage.processed += 1
                    audits[business_id] = audit

                    business = await repo.get(business_id)
                    if business is None:
                        continue
                    if audit is not None:
                        await stages.save_audit(session, business, audit, self._run_id)
                        stage.succeeded += 1
                        if audit.emails and not business.email:
                            business.email = audit.emails[0][:320]
                        if audit.social_links:
                            business.social_links = {
                                **(business.social_links or {}),
                                **audit.social_links,
                            }
                    else:
                        stage.skipped += 1
                    if contact_rows:
                        await stages.save_contacts(session, business_id, contact_rows)

            log.info("audit.batch", done=stage.processed, total=len(business_ids))

        self.result.audited = stage.succeeded
        stage.notes = {"browser_renders": getattr(browser, "renders", 0)}
        self._end(stage)
        return audits

    async def _stage_score(self, audits: dict[int, WebsiteAudit | None]) -> None:
        stage = self._begin("score")
        if self.config.dry_run or not audits:
            self._end(stage)
            return

        catalog = get_niches()
        qualified = 0

        for batch_ids in stages.chunk(list(audits.keys()), 100):
            async with session_scope() as session:
                repo = BusinessRepository(session)
                for business_id in batch_ids:
                    business = await repo.get_with_relations(business_id)
                    if business is None:
                        continue
                    audit = audits.get(business_id)

                    niche = None
                    if business.niche_key:
                        with contextlib.suppress(KeyError):
                            niche = catalog.get(business.niche_key)

                    has_email = bool(business.email) or bool(
                        [c for c in business.contacts if c.kind == "email"]
                    )
                    result = score_lead(
                        business,
                        audit,
                        niche,
                        has_email=has_email,
                        has_phone=bool(business.phone_e164),
                    )
                    await stages.save_lead(session, business, result, audit, self._run_id)
                    stage.processed += 1
                    if result.qualified:
                        qualified += 1

        stage.succeeded = stage.processed
        self.result.scored = stage.processed
        self.result.qualified = qualified
        self._end(stage)
        log.info("score.complete", scored=stage.processed, qualified=qualified)

    async def _stage_ai(self, audits: dict[int, WebsiteAudit | None]) -> None:
        stage = self._begin("ai_enrich")
        if self.config.dry_run or not self.config.use_ai:
            self._end(stage)
            return

        service = EnrichmentService(AIClient())
        if not service.enabled:
            log.info("ai.disabled")
            stage.skipped = len(audits)
            self._end(stage)
            return

        catalog = get_niches()

        async with session_scope() as session:
            leads = await LeadRepository(session).search(
                qualified_only=True,
                order_by="score",
                limit=self.config.ai_enrich_top_n,
            )
            candidates = [(lead.id, lead.business_id) for lead in leads]

        items: list[dict[str, Any]] = []
        meta: list[tuple[int, str]] = []

        async with session_scope() as session:
            business_repo = BusinessRepository(session)
            insight_repo = InsightRepository(session)
            audit_repo = AuditRepository(session)

            for _lead_id, business_id in candidates:
                business = await business_repo.get(business_id)
                if business is None:
                    continue

                audit = audits.get(business_id)
                latest = await audit_repo.latest_for_business(business_id)
                problems = list(audit.problems) if audit else list(latest.problems or []) if latest else []
                website_score = audit.score if audit else (latest.score if latest else None)

                input_hash = compute_input_hash(
                    name=business.name,
                    website=business.website_url,
                    website_status=str(business.lead.website_status if business.lead else "unknown"),
                    website_score=website_score,
                    problems=problems,
                    review_count=business.review_count,
                )
                if await insight_repo.find_by_hash(business_id, input_hash):
                    service.skipped += 1
                    stage.skipped += 1
                    continue

                niche = None
                if business.niche_key:
                    with contextlib.suppress(KeyError):
                        niche = catalog.get(business.niche_key)

                items.append(
                    {
                        "name": business.name,
                        "niche": niche,
                        "city": business.city,
                        "state": business.state,
                        "categories": list(business.categories or []),
                        "rating": business.rating,
                        "review_count": business.review_count,
                        "website": business.website_url,
                        "audit": audit,
                        "has_phone": bool(business.phone_e164),
                        "hours_published": bool(business.hours),
                    }
                )
                meta.append((business_id, input_hash))

        if not items:
            log.info("ai.nothing_to_enrich", skipped_cached=stage.skipped)
            self._end(stage)
            return

        results = await service.enrich_many(items)

        async with session_scope() as session:
            for (business_id, input_hash), enrichment in zip(meta, results, strict=True):
                stage.processed += 1
                if enrichment is None:
                    stage.failed += 1
                    continue
                await stages.save_insight(session, business_id, enrichment, input_hash, self._run_id)
                stage.succeeded += 1

        self.result.enriched = stage.succeeded
        stage.notes = service.stats()
        self._end(stage)
        log.info("ai.complete", **service.stats())

    # ------------------------------------------------------------------
    # Run bookkeeping
    # ------------------------------------------------------------------

    def _make_browser(self) -> BrowserAuditor | NullBrowserAuditor:
        want_browser = (
            self.settings.enable_browser_audit
            if self.config.use_browser is None
            else self.config.use_browser
        )
        if not want_browser:
            return NullBrowserAuditor()
        try:
            import playwright  # noqa: F401
        except ImportError:
            log.warning(
                "browser.unavailable",
                hint="playwright is not installed; running HTTP-only audits",
            )
            return NullBrowserAuditor()
        return BrowserAuditor()

    async def _open_run(self, location_key: str, niches: list[Niche]) -> None:
        if self.config.dry_run:
            return
        async with session_scope() as session:
            run = await RunRepository(session).create(
                run_date=self.result.run_date,
                trigger=self.config.trigger,
                location_key=location_key,
                niche_keys=[n.key for n in niches],
                target_leads=self.config.target_leads,
                config_snapshot={
                    "per_cell_limit": self.config.per_cell_limit,
                    "discovery_cap": self.config.discovery_cap
                    or self.settings.daily_discovery_cap,
                    "use_browser": self.config.use_browser,
                    "use_pagespeed": self.config.use_pagespeed,
                    "use_ai": self.config.use_ai,
                    "providers": self.settings.provider_order,
                },
            )
            self._run_id = run.id
            self.result.run_id = run.id

    async def _close_run(self) -> None:
        if self.config.dry_run or self._run_id is None:
            return
        from dataclasses import asdict

        async with session_scope() as session:
            await RunRepository(session).finish(
                self._run_id,
                status=self.result.status,
                discovered=self.result.discovered,
                new_businesses=self.result.new_businesses,
                duplicates=self.result.duplicates,
                audited=self.result.audited,
                scored=self.result.scored,
                qualified=self.result.qualified,
                enriched=self.result.enriched,
                api_calls=self.result.api_calls,
                stages=[_stage_dict(s) for s in self.result.stages],
                errors=self.result.errors[:50],
            )
        _ = asdict

    def _begin(self, name: str) -> StageStats:
        stage = StageStats(name=name, started_at=datetime.utcnow())
        log.info("stage.start", stage=name)
        return stage

    def _end(self, stage: StageStats) -> None:
        stage.finished_at = datetime.utcnow()
        self.result.stages.append(stage)
        log.info(
            "stage.end",
            stage=stage.name,
            processed=stage.processed,
            succeeded=stage.succeeded,
            failed=stage.failed,
            skipped=stage.skipped,
            seconds=round(stage.duration_s, 1),
        )


def _stage_dict(stage: StageStats) -> dict[str, Any]:
    return {
        "name": stage.name,
        "processed": stage.processed,
        "succeeded": stage.succeeded,
        "failed": stage.failed,
        "skipped": stage.skipped,
        "duration_s": round(stage.duration_s, 2),
        "notes": stage.notes,
    }


async def run_daily(config: PipelineConfig | None = None) -> RunResult:
    """Entry point used by the CLI, the scheduler, and the API."""
    pipeline = DailyPipeline(config)
    return await pipeline.run()
