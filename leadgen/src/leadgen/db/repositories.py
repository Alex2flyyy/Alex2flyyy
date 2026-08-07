"""Data access.

All SQL lives here. The pipeline, API, and exporters call repository methods and
never build queries themselves, so index-affecting changes have exactly one
place to happen.

The upsert methods use Postgres ``INSERT ... ON CONFLICT`` rather than
select-then-insert. That is not a micro-optimization: two pipeline workers can
process the same business concurrently, and only the database can arbitrate
that race.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, delete, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from leadgen.db.models import (
    AIInsight,
    Business,
    Contact,
    DailyReport,
    ExportJob,
    GeocodeCache,
    GeoPlace,
    Lead,
    LeadScoreHistory,
    OutreachActivity,
    PipelineRun,
    RobotsCache,
    Suppression,
    WebsiteAuditRecord,
    WebsiteStatusChange,
)
from leadgen.domain import LeadStatus, RunStatus, WebsiteStatus
from leadgen.logging import get_logger

log = get_logger(__name__)


# =============================================================================
# Businesses
# =============================================================================


class BusinessRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, business_id: int) -> Business | None:
        return await self.session.get(Business, business_id)

    async def get_with_relations(self, business_id: int) -> Business | None:
        stmt = (
            select(Business)
            .where(Business.id == business_id)
            .options(
                selectinload(Business.lead),
                selectinload(Business.contacts),
                selectinload(Business.audits),
                selectinload(Business.insights),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_source_key(self, source_key: str) -> Business | None:
        stmt = select(Business).where(Business.source_key == source_key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_dedupe_key(self, dedupe_key: str) -> Business | None:
        stmt = select(Business).where(Business.dedupe_key == dedupe_key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_probable_duplicate(
        self, *, phone_e164: str | None, website_domain: str | None, normalized_name: str
    ) -> Business | None:
        """Second-chance dedupe for records the hash key misses.

        A business that moved suites, or that one provider lists as
        "Joe's Plumbing" and another as "Joes Plumbing Inc", produces a
        different dedupe hash. Phone number and website domain are far more
        stable identifiers, so we check those before creating a new row.
        """
        clauses = []
        if phone_e164:
            clauses.append(Business.phone_e164 == phone_e164)
        if website_domain:
            clauses.append(
                and_(
                    Business.website_domain == website_domain,
                    Business.normalized_name == normalized_name,
                )
            )
        if not clauses:
            return None
        stmt = select(Business).where(or_(*clauses)).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(self, values: dict[str, Any]) -> tuple[Business, bool]:
        """Insert or refresh a business. Returns ``(business, created)``.

        On conflict we deliberately refresh only volatile listing fields
        (rating, review count, hours, website) and never touch
        ``first_discovered_at`` — that column is how "new today" is computed.
        """
        now = datetime.utcnow()
        payload = {**values, "last_seen_at": now}

        refreshable = {
            "name",
            "normalized_name",
            "phone",
            "phone_e164",
            "website_url",
            "website_domain",
            "street_address",
            "city",
            "county",
            "state",
            "zip_code",
            "lat",
            "lng",
            "google_maps_url",
            "categories",
            "rating",
            "review_count",
            "price_level",
            "hours",
            "business_status",
            "description",
            "niche_key",
            "is_chain",
            "last_seen_at",
            "raw_payload",
        }
        update_set = {k: v for k, v in payload.items() if k in refreshable}

        # `xmax = 0` is the standard Postgres idiom for "this RETURNING row came
        # from the INSERT branch, not the UPDATE branch". It is the only way to
        # distinguish the two in a single statement, and a single statement is
        # what keeps concurrent workers from both claiming they created it.
        stmt = (
            pg_insert(Business)
            .values(**payload)
            .on_conflict_do_update(
                index_elements=[Business.source_key],
                set_=update_set,
            )
            .returning(Business.id, literal_column("xmax = 0").label("inserted"))
        )
        row = (await self.session.execute(stmt)).one()
        business = await self.session.get(Business, row.id)
        assert business is not None
        return business, bool(row.inserted)

    async def touch_checked(self, business_id: int) -> None:
        await self.session.execute(
            update(Business)
            .where(Business.id == business_id)
            .values(last_checked_at=datetime.utcnow())
        )

    async def stale_for_recheck(self, older_than_days: int, limit: int) -> Sequence[Business]:
        """Businesses whose website hasn't been looked at recently.

        Feeds the re-audit stage. A prospect whose site broke last week is a
        better call than a cold record you've never touched.
        """
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        stmt = (
            select(Business)
            .where(
                or_(Business.last_checked_at.is_(None), Business.last_checked_at < cutoff),
                Business.website_url.is_not(None),
            )
            .order_by(Business.last_checked_at.asc().nulls_first())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count(Business.id)))).scalar() or 0)


# =============================================================================
# Website audits
# =============================================================================


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest_for_business(self, business_id: int) -> WebsiteAuditRecord | None:
        stmt = (
            select(WebsiteAuditRecord)
            .where(WebsiteAuditRecord.business_id == business_id)
            .order_by(WebsiteAuditRecord.checked_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, record: WebsiteAuditRecord) -> WebsiteAuditRecord:
        self.session.add(record)
        await self.session.flush()
        return record

    async def record_status_change(
        self,
        *,
        business_id: int,
        old: WebsiteAuditRecord | None,
        new_status: WebsiteStatus,
        new_score: float,
        new_url: str | None,
    ) -> WebsiteStatusChange | None:
        """Write a change row only when something actually moved."""
        old_status = old.status if old else None
        old_score = old.score if old else None
        if old_status == new_status and old and abs((old_score or 0) - new_score) < 5:
            return None
        change = WebsiteStatusChange(
            business_id=business_id,
            old_status=old_status,
            new_status=new_status,
            old_score=old_score,
            new_score=new_score,
            old_url=old.url if old else None,
            new_url=new_url,
        )
        self.session.add(change)
        return change

    async def recent_changes(
        self, days: int = 7, limit: int = 100
    ) -> Sequence[WebsiteStatusChange]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        stmt = (
            select(WebsiteStatusChange)
            .where(WebsiteStatusChange.detected_at >= cutoff)
            .order_by(WebsiteStatusChange.detected_at.desc())
            .limit(limit)
            .options(selectinload(WebsiteStatusChange.business))
        )
        return (await self.session.execute(stmt)).scalars().all()


# =============================================================================
# Leads
# =============================================================================


class LeadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, lead_id: int) -> Lead | None:
        return await self.session.get(Lead, lead_id)

    async def get_full(self, lead_id: int) -> Lead | None:
        stmt = (
            select(Lead)
            .where(Lead.id == lead_id)
            .options(
                selectinload(Lead.business).selectinload(Business.contacts),
                selectinload(Lead.business).selectinload(Business.insights),
                selectinload(Lead.business).selectinload(Business.audits),
                selectinload(Lead.activities),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_business(self, business_id: int) -> Lead | None:
        stmt = select(Lead).where(Lead.business_id == business_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_score(
        self,
        *,
        business_id: int,
        score: int,
        website_status: WebsiteStatus,
        website_score: float | None,
        qualified: bool,
        reason: str,
        components: list[dict],
        adjustments: list[dict],
        run_id: int | None = None,
    ) -> tuple[Lead, bool]:
        """Create or re-score a lead.

        Sales-workflow columns (status, notes, follow-ups) are never written
        here. Re-scoring a lead you already called must not reset it to "new".
        """
        now = datetime.utcnow()
        existing = await self.get_by_business(business_id)

        if existing is None:
            lead = Lead(
                business_id=business_id,
                score=score,
                website_status=website_status,
                website_score=website_score,
                qualified=qualified,
                reason=reason,
                score_components=components,
                score_adjustments=adjustments,
                status=LeadStatus.QUALIFIED if qualified else LeadStatus.NEW,
                priority=_priority_for(score),
                first_scored_at=now,
                last_scored_at=now,
            )
            self.session.add(lead)
            await self.session.flush()
            created = True
        else:
            lead = existing
            lead.previous_score = lead.score
            lead.score = score
            lead.website_status = website_status
            lead.website_score = website_score
            lead.qualified = qualified
            lead.reason = reason
            lead.score_components = components
            lead.score_adjustments = adjustments
            lead.priority = _priority_for(score)
            lead.last_scored_at = now
            # Promote an untouched lead that just crossed the bar; leave any
            # lead already in the sales process exactly where it is.
            if qualified and lead.status == LeadStatus.NEW:
                lead.status = LeadStatus.QUALIFIED
            created = False

        self.session.add(
            LeadScoreHistory(
                lead_id=lead.id,
                run_id=run_id,
                score=score,
                website_score=website_score,
                website_status=website_status,
                components=components,
            )
        )
        return lead, created

    def _base_query(self) -> Select[tuple[Lead]]:
        return select(Lead).options(selectinload(Lead.business))

    @staticmethod
    def _filters(
        *,
        min_score: int | None = None,
        max_score: int | None = None,
        statuses: Iterable[LeadStatus] | None = None,
        website_statuses: Iterable[WebsiteStatus] | None = None,
        niches: Iterable[str] | None = None,
        cities: Iterable[str] | None = None,
        states: Iterable[str] | None = None,
        zips: Iterable[str] | None = None,
        qualified_only: bool = False,
        has_phone: bool | None = None,
        has_email: bool | None = None,
        discovered_on: date | None = None,
        discovered_since: date | None = None,
        query: str | None = None,
    ) -> list[Any]:
        """Build the WHERE clauses shared by ``search`` and ``count_search``.

        Kept as one function so a filter can never mean two different things
        depending on whether you asked for rows or for a count.
        """
        clauses: list[Any] = []
        if min_score is not None:
            clauses.append(Lead.score >= min_score)
        if max_score is not None:
            clauses.append(Lead.score <= max_score)
        if statuses:
            clauses.append(Lead.status.in_(list(statuses)))
        if website_statuses:
            clauses.append(Lead.website_status.in_(list(website_statuses)))
        if niches:
            clauses.append(Business.niche_key.in_(list(niches)))
        if cities:
            clauses.append(Business.city.in_(list(cities)))
        if states:
            clauses.append(Business.state.in_(list(states)))
        if zips:
            clauses.append(Business.zip_code.in_(list(zips)))
        if qualified_only:
            clauses.append(Lead.qualified.is_(True))
        if has_phone is True:
            clauses.append(Business.phone_e164.is_not(None))
        elif has_phone is False:
            clauses.append(Business.phone_e164.is_(None))
        if has_email is True:
            clauses.append(Business.email.is_not(None))
        elif has_email is False:
            clauses.append(Business.email.is_(None))
        if discovered_on:
            clauses.append(func.date(Business.first_discovered_at) == discovered_on)
        if discovered_since:
            clauses.append(func.date(Business.first_discovered_at) >= discovered_since)
        if query:
            like = f"%{query.lower()}%"
            clauses.append(
                or_(
                    func.lower(Business.name).like(like),
                    func.lower(Business.city).like(like),
                    Business.phone_e164.like(like),
                    func.lower(Business.website_domain).like(like),
                )
            )
        return clauses

    async def search(
        self,
        *,
        min_score: int | None = None,
        max_score: int | None = None,
        statuses: Iterable[LeadStatus] | None = None,
        website_statuses: Iterable[WebsiteStatus] | None = None,
        niches: Iterable[str] | None = None,
        cities: Iterable[str] | None = None,
        states: Iterable[str] | None = None,
        zips: Iterable[str] | None = None,
        qualified_only: bool = False,
        has_phone: bool | None = None,
        has_email: bool | None = None,
        discovered_on: date | None = None,
        discovered_since: date | None = None,
        query: str | None = None,
        order_by: str = "score",
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Lead]:
        clauses = self._filters(
            min_score=min_score,
            max_score=max_score,
            statuses=statuses,
            website_statuses=website_statuses,
            niches=niches,
            cities=cities,
            states=states,
            zips=zips,
            qualified_only=qualified_only,
            has_phone=has_phone,
            has_email=has_email,
            discovered_on=discovered_on,
            discovered_since=discovered_since,
            query=query,
        )
        stmt = self._base_query().join(Business, Lead.business_id == Business.id).where(*clauses)

        ordering = {
            "score": Lead.score.desc(),
            "score_asc": Lead.score.asc(),
            "newest": Business.first_discovered_at.desc(),
            "oldest": Business.first_discovered_at.asc(),
            "name": Business.name.asc(),
            "city": Business.city.asc(),
            "updated": Lead.last_scored_at.desc(),
        }
        stmt = stmt.order_by(ordering.get(order_by, Lead.score.desc()), Lead.id.desc())
        stmt = stmt.limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count_search(self, **kwargs: Any) -> int:
        for pagination_arg in ("limit", "offset", "order_by"):
            kwargs.pop(pagination_arg, None)
        stmt = (
            select(func.count(Lead.id))
            .join(Business, Lead.business_id == Business.id)
            .where(*self._filters(**kwargs))
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def top_for_date(self, day: date, limit: int = 50) -> Sequence[Lead]:
        """The daily list: today's discoveries first, backfilled with the best
        open leads so the number is always hit even on a thin discovery day."""
        todays = await self.search(
            discovered_on=day, qualified_only=True, order_by="score", limit=limit
        )
        if len(todays) >= limit:
            return todays

        seen = {lead.id for lead in todays}
        backfill = await self.search(
            qualified_only=True,
            statuses=[LeadStatus.NEW, LeadStatus.QUALIFIED],
            order_by="score",
            limit=limit * 3,
        )
        merged = list(todays)
        for lead in backfill:
            if lead.id in seen:
                continue
            merged.append(lead)
            if len(merged) >= limit:
                break
        return merged

    async def update_status(
        self,
        lead_id: int,
        status: LeadStatus,
        *,
        notes: str | None = None,
        follow_up_at: datetime | None = None,
        channel: str | None = None,
        deal_value: float | None = None,
        lost_reason: str | None = None,
    ) -> Lead | None:
        lead = await self.get(lead_id)
        if lead is None:
            return None
        now = datetime.utcnow()
        lead.status = status
        if notes is not None:
            lead.notes = notes
        if follow_up_at is not None:
            lead.follow_up_at = follow_up_at
        if status == LeadStatus.CONTACTED:
            lead.contacted_at = lead.contacted_at or now
            lead.contact_attempts += 1
            lead.last_contact_channel = channel or lead.last_contact_channel
        elif status == LeadStatus.RESPONDED:
            lead.responded_at = now
        elif status == LeadStatus.WON:
            lead.won_at = now
            lead.deal_value = deal_value
        elif status == LeadStatus.LOST:
            lead.lost_at = now
            lead.lost_reason = lost_reason
        return lead

    async def due_follow_ups(self, limit: int = 100) -> Sequence[Lead]:
        stmt = (
            self._base_query()
            .where(
                Lead.follow_up_at.is_not(None),
                Lead.follow_up_at <= datetime.utcnow(),
                Lead.status.in_([LeadStatus.CONTACTED, LeadStatus.RESPONDED]),
            )
            .order_by(Lead.follow_up_at.asc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()


def _priority_for(score: int) -> int:
    if score >= 85:
        return 1
    if score >= 72:
        return 2
    if score >= 60:
        return 3
    if score >= 45:
        return 4
    return 5


# =============================================================================
# Contacts / insights
# =============================================================================


def _align_keys(rows: list[dict[str, Any]], model: Any) -> list[dict[str, Any]]:
    """Give every row in a multi-row INSERT the same set of keys.

    SQLAlchemy compiles a multi-row ``VALUES`` clause from the *first* row's
    keys, then requires every later row to supply those same keys. A row that
    omits one raises ``CompileError`` at compile time unless the column has a
    Python-side default it can substitute.

    Callers naturally produce heterogeneous dicts — a phone contact has no
    ``label``, a social link has no ``person_name`` — so normalizing belongs
    here rather than in every caller, where forgetting it is silent until the
    one run that happens to find both a phone and an email.

    Missing keys take the column's Python-side default when it has a scalar
    one, so ``source`` and ``confidence`` still get their declared values
    instead of being written as NULL.
    """
    keys = {key for row in rows for key in row}
    columns = model.__table__.columns
    filler: dict[str, Any] = {}
    for key in keys:
        column = columns.get(key)
        default = getattr(column, "default", None) if column is not None else None
        # Callable and SQL-expression defaults cannot be materialized here;
        # leaving them None lets the database apply its own.
        filler[key] = default.arg if default is not None and default.is_scalar else None
    return [{**filler, **row} for row in rows]


class ContactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_many(self, business_id: int, contacts: list[dict[str, Any]]) -> int:
        if not contacts:
            return 0
        rows = _align_keys(
            [{**c, "business_id": business_id} for c in contacts],
            Contact,
        )
        stmt = (
            pg_insert(Contact)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["business_id", "kind", "value"])
        )
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    async def for_business(self, business_id: int) -> Sequence[Contact]:
        stmt = select(Contact).where(Contact.business_id == business_id)
        return (await self.session.execute(stmt)).scalars().all()


class InsightRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest(self, business_id: int) -> AIInsight | None:
        stmt = (
            select(AIInsight)
            .where(AIInsight.business_id == business_id)
            .order_by(AIInsight.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_by_hash(self, business_id: int, input_hash: str) -> AIInsight | None:
        stmt = select(AIInsight).where(
            AIInsight.business_id == business_id, AIInsight.input_hash == input_hash
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, insight: AIInsight) -> AIInsight:
        self.session.add(insight)
        await self.session.flush()
        return insight


# =============================================================================
# Runs / reports
# =============================================================================


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        run_date: date,
        trigger: str,
        location_key: str | None,
        niche_keys: list[str],
        target_leads: int,
        config_snapshot: dict[str, Any],
    ) -> PipelineRun:
        run = PipelineRun(
            run_date=run_date,
            trigger=trigger,
            status=RunStatus.RUNNING,
            location_key=location_key,
            niche_keys=niche_keys,
            target_leads=target_leads,
            config_snapshot=config_snapshot,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish(self, run_id: int, **fields: Any) -> None:
        finished = datetime.utcnow()
        run = await self.session.get(PipelineRun, run_id)
        if run is None:
            return
        for k, v in fields.items():
            setattr(run, k, v)
        run.finished_at = finished
        run.duration_seconds = (finished - run.started_at.replace(tzinfo=None)).total_seconds()

    async def recent(self, limit: int = 20) -> Sequence[PipelineRun]:
        stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def latest_for_date(self, day: date) -> PipelineRun | None:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.run_date == day)
            .order_by(PipelineRun.started_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, report_date: date, values: dict[str, Any]) -> DailyReport:
        stmt = (
            pg_insert(DailyReport)
            .values(report_date=report_date, **values)
            .on_conflict_do_update(index_elements=["report_date"], set_=values)
            .returning(DailyReport.id)
        )
        report_id = (await self.session.execute(stmt)).scalar_one()
        report = await self.session.get(DailyReport, report_id)
        assert report is not None
        return report

    async def get(self, report_date: date) -> DailyReport | None:
        stmt = select(DailyReport).where(DailyReport.report_date == report_date)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def recent(self, limit: int = 30) -> Sequence[DailyReport]:
        stmt = select(DailyReport).order_by(DailyReport.report_date.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()


# =============================================================================
# Suppression
# =============================================================================


class SuppressionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_values(self) -> dict[str, set[str]]:
        now = datetime.utcnow()
        stmt = select(Suppression).where(
            or_(Suppression.expires_at.is_(None), Suppression.expires_at > now)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        out: dict[str, set[str]] = {}
        for row in rows:
            out.setdefault(row.kind, set()).add(row.value.lower())
        return out

    async def add(self, kind: str, value: str, reason: str | None = None) -> None:
        stmt = (
            pg_insert(Suppression)
            .values(kind=kind, value=value, reason=reason)
            .on_conflict_do_nothing(index_elements=["kind", "value"])
        )
        await self.session.execute(stmt)

    async def remove(self, kind: str, value: str) -> None:
        await self.session.execute(
            delete(Suppression).where(Suppression.kind == kind, Suppression.value == value)
        )


# =============================================================================
# Geography
# =============================================================================


class GeoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def places_for_state(
        self, state: str, *, min_population: int = 0, limit: int = 200
    ) -> Sequence[GeoPlace]:
        stmt = (
            select(GeoPlace)
            .where(GeoPlace.state == state.upper(), GeoPlace.population >= min_population)
            .order_by(GeoPlace.population.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def places_for_county(
        self, county: str, state: str, *, min_population: int = 0, limit: int = 200
    ) -> Sequence[GeoPlace]:
        stmt = (
            select(GeoPlace)
            .where(
                func.lower(GeoPlace.county) == county.lower(),
                GeoPlace.state == state.upper(),
                GeoPlace.population >= min_population,
            )
            .order_by(GeoPlace.population.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def places_for_country(
        self, country_code: str = "US", *, min_population: int = 0, limit: int = 500
    ) -> Sequence[GeoPlace]:
        stmt = (
            select(GeoPlace)
            .where(
                GeoPlace.country_code == country_code.upper(), GeoPlace.population >= min_population
            )
            .order_by(GeoPlace.population.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def find_place(self, name: str, state: str) -> GeoPlace | None:
        from leadgen.enrichment.normalize import normalize_name

        stmt = select(GeoPlace).where(
            GeoPlace.normalized_name == normalize_name(name), GeoPlace.state == state.upper()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def bulk_upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        stmt = (
            pg_insert(GeoPlace)
            .values(rows)
            .on_conflict_do_update(
                index_elements=["normalized_name", "state", "country_code"],
                set_={
                    "lat": pg_insert(GeoPlace).excluded.lat,
                    "lng": pg_insert(GeoPlace).excluded.lng,
                    "population": pg_insert(GeoPlace).excluded.population,
                    "county": pg_insert(GeoPlace).excluded.county,
                    "zips": pg_insert(GeoPlace).excluded.zips,
                },
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count(GeoPlace.id)))).scalar() or 0)

    # -- geocode cache --

    async def cached_geocode(self, query: str) -> GeocodeCache | None:
        stmt = select(GeocodeCache).where(GeocodeCache.query == query)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def cache_geocode(self, query: str, values: dict[str, Any]) -> None:
        stmt = (
            pg_insert(GeocodeCache)
            .values(query=query, **values)
            .on_conflict_do_update(index_elements=["query"], set_=values)
        )
        await self.session.execute(stmt)

    # -- robots cache --

    async def cached_robots(self, host: str, max_age_hours: int = 24) -> RobotsCache | None:
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        stmt = select(RobotsCache).where(RobotsCache.host == host, RobotsCache.fetched_at >= cutoff)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def cache_robots(self, host: str, body: str | None, status_code: int | None) -> None:
        values = {"body": body, "status_code": status_code, "fetched_at": datetime.utcnow()}
        stmt = (
            pg_insert(RobotsCache)
            .values(host=host, **values)
            .on_conflict_do_update(index_elements=["host"], set_=values)
        )
        await self.session.execute(stmt)


# =============================================================================
# Analytics for the dashboard
# =============================================================================


class AnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def overview(self, day: date | None = None) -> dict[str, Any]:
        day = day or date.today()
        total_leads = (await self.session.execute(select(func.count(Lead.id)))).scalar() or 0
        total_businesses = (
            await self.session.execute(select(func.count(Business.id)))
        ).scalar() or 0
        qualified = (
            await self.session.execute(select(func.count(Lead.id)).where(Lead.qualified.is_(True)))
        ).scalar() or 0
        new_today = (
            await self.session.execute(
                select(func.count(Business.id)).where(
                    func.date(Business.first_discovered_at) == day
                )
            )
        ).scalar() or 0
        contacted = (
            await self.session.execute(
                select(func.count(Lead.id)).where(Lead.contacted_at.is_not(None))
            )
        ).scalar() or 0
        pending = (
            await self.session.execute(
                select(func.count(Lead.id)).where(
                    Lead.qualified.is_(True),
                    Lead.status.in_([LeadStatus.NEW, LeadStatus.QUALIFIED]),
                )
            )
        ).scalar() or 0
        won = (
            await self.session.execute(
                select(func.count(Lead.id)).where(Lead.status == LeadStatus.WON)
            )
        ).scalar() or 0
        lost = (
            await self.session.execute(
                select(func.count(Lead.id)).where(Lead.status == LeadStatus.LOST)
            )
        ).scalar() or 0
        avg_score = (
            await self.session.execute(select(func.avg(Lead.score)).where(Lead.qualified.is_(True)))
        ).scalar()
        revenue = (
            await self.session.execute(
                select(func.coalesce(func.sum(Lead.deal_value), 0)).where(
                    Lead.status == LeadStatus.WON
                )
            )
        ).scalar() or 0

        return {
            "date": day.isoformat(),
            "total_businesses": int(total_businesses),
            "total_leads": int(total_leads),
            "qualified_leads": int(qualified),
            "new_today": int(new_today),
            "contacted": int(contacted),
            "pending": int(pending),
            "won": int(won),
            "lost": int(lost),
            "avg_score": round(float(avg_score), 1) if avg_score else 0.0,
            "contact_rate": round(contacted / qualified * 100, 1) if qualified else 0.0,
            "win_rate": round(won / contacted * 100, 1) if contacted else 0.0,
            "revenue": float(revenue),
        }

    async def by_city(self, limit: int = 20) -> list[dict[str, Any]]:
        stmt = (
            select(
                Business.city,
                Business.state,
                func.count(Lead.id).label("leads"),
                func.avg(Lead.score).label("avg_score"),
            )
            .join(Lead, Lead.business_id == Business.id)
            .where(Business.city.is_not(None))
            .group_by(Business.city, Business.state)
            .order_by(func.count(Lead.id).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "city": r.city,
                "state": r.state,
                "leads": int(r.leads),
                "avg_score": round(float(r.avg_score or 0), 1),
            }
            for r in rows
        ]

    async def by_niche(self, limit: int = 40) -> list[dict[str, Any]]:
        stmt = (
            select(
                Business.niche_key,
                func.count(Lead.id).label("leads"),
                func.avg(Lead.score).label("avg_score"),
                func.count(func.nullif(Lead.qualified, False)).label("qualified"),
            )
            .join(Lead, Lead.business_id == Business.id)
            .where(Business.niche_key.is_not(None))
            .group_by(Business.niche_key)
            .order_by(func.count(Lead.id).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "niche": r.niche_key,
                "leads": int(r.leads),
                "qualified": int(r.qualified or 0),
                "avg_score": round(float(r.avg_score or 0), 1),
            }
            for r in rows
        ]

    async def by_website_status(self) -> list[dict[str, Any]]:
        stmt = (
            select(Lead.website_status, func.count(Lead.id).label("count"))
            .group_by(Lead.website_status)
            .order_by(func.count(Lead.id).desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "status": r.website_status.value if r.website_status else "unknown",
                "display": r.website_status.display if r.website_status else "Unknown",
                "count": int(r.count),
            }
            for r in rows
        ]

    async def score_distribution(self) -> list[dict[str, Any]]:
        buckets = [(0, 39), (40, 54), (55, 69), (70, 84), (85, 100)]
        out = []
        for lo, hi in buckets:
            count = (
                await self.session.execute(
                    select(func.count(Lead.id)).where(Lead.score >= lo, Lead.score <= hi)
                )
            ).scalar() or 0
            out.append({"bucket": f"{lo}-{hi}", "count": int(count)})
        return out

    async def by_status(self) -> list[dict[str, Any]]:
        stmt = (
            select(Lead.status, func.count(Lead.id).label("count"))
            .group_by(Lead.status)
            .order_by(func.count(Lead.id).desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [{"status": r.status.value, "count": int(r.count)} for r in rows]

    async def daily_trend(self, days: int = 30) -> list[dict[str, Any]]:
        since = date.today() - timedelta(days=days)
        stmt = (
            select(
                func.date(Business.first_discovered_at).label("day"),
                func.count(Business.id).label("discovered"),
            )
            .where(func.date(Business.first_discovered_at) >= since)
            .group_by(func.date(Business.first_discovered_at))
            .order_by(func.date(Business.first_discovered_at))
        )
        rows = (await self.session.execute(stmt)).all()
        return [{"day": str(r.day), "discovered": int(r.discovered)} for r in rows]

    async def conversion_funnel(self) -> list[dict[str, Any]]:
        stages = [
            ("Discovered", select(func.count(Business.id))),
            ("Qualified", select(func.count(Lead.id)).where(Lead.qualified.is_(True))),
            ("Contacted", select(func.count(Lead.id)).where(Lead.contacted_at.is_not(None))),
            ("Responded", select(func.count(Lead.id)).where(Lead.responded_at.is_not(None))),
            (
                "Meeting",
                select(func.count(Lead.id)).where(
                    Lead.status.in_(
                        [LeadStatus.MEETING_BOOKED, LeadStatus.PROPOSAL_SENT, LeadStatus.WON]
                    )
                ),
            ),
            ("Won", select(func.count(Lead.id)).where(Lead.status == LeadStatus.WON)),
        ]
        out = []
        for label, stmt in stages:
            count = (await self.session.execute(stmt)).scalar() or 0
            out.append({"stage": label, "count": int(count)})
        return out


class ExportJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, destination: str, filters: dict[str, Any]) -> ExportJob:
        job = ExportJob(destination=destination, filters=filters, status="running")
        self.session.add(job)
        await self.session.flush()
        return job

    async def finish(
        self,
        job_id: int,
        *,
        status: str,
        row_count: int = 0,
        output_path: str | None = None,
        remote_ref: str | None = None,
        error: str | None = None,
    ) -> None:
        job = await self.session.get(ExportJob, job_id)
        if job is None:
            return
        job.status = status
        job.row_count = row_count
        job.output_path = output_path
        job.remote_ref = remote_ref
        job.error = error
        job.finished_at = datetime.utcnow()

    async def recent(self, limit: int = 20) -> Sequence[ExportJob]:
        stmt = select(ExportJob).order_by(ExportJob.created_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()


class ActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        lead_id: int,
        channel: str,
        *,
        subject: str | None = None,
        body: str | None = None,
        outcome: str | None = None,
        direction: str = "outbound",
        payload: dict[str, Any] | None = None,
    ) -> OutreachActivity:
        activity = OutreachActivity(
            lead_id=lead_id,
            channel=channel,
            direction=direction,
            subject=subject,
            body=body,
            outcome=outcome,
            payload=payload or {},
        )
        self.session.add(activity)
        await self.session.flush()
        return activity
