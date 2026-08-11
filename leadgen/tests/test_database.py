"""Database integration tests.

These verify the behavior that only a real database can demonstrate: that the
unique constraints actually stop duplicates, that ``ON CONFLICT`` correctly
reports insert-versus-update, and that re-scoring a lead does not destroy sales
workflow state.

Skipped automatically when ``TEST_DATABASE_URL`` is not set.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from tests.conftest import requires_db

from leadgen.config import get_scoring
from leadgen.db.models import Business, WebsiteAuditRecord
from leadgen.db.repositories import (
    AnalyticsRepository,
    AuditRepository,
    BusinessRepository,
    ContactRepository,
    LeadRepository,
    RunRepository,
    SuppressionRepository,
)
from leadgen.domain import AuditOutcome, LeadStatus, RunStatus, WebsiteStatus
from leadgen.pipeline.stages import business_values

pytestmark = requires_db


async def _make_business(session, raw, **overrides) -> Business:
    values = business_values(raw, location_key="test", scoring_config=get_scoring())
    values.update(overrides)
    business, _created = await BusinessRepository(session).upsert(values)
    return business


class TestBusinessUpsert:
    async def test_first_insert_reports_created(self, session, raw_business) -> None:
        values = business_values(raw_business, location_key="t", scoring_config=get_scoring())
        _business, created = await BusinessRepository(session).upsert(values)
        assert created is True

    async def test_second_insert_reports_updated(self, session, raw_business) -> None:
        repo = BusinessRepository(session)
        values = business_values(raw_business, location_key="t", scoring_config=get_scoring())
        await repo.upsert(values)
        _business, created = await repo.upsert(values)
        assert created is False

    async def test_volatile_fields_refresh(self, session, raw_business) -> None:
        repo = BusinessRepository(session)
        values = business_values(raw_business, location_key="t", scoring_config=get_scoring())
        await repo.upsert(values)

        raw_business.rating = 4.9
        raw_business.review_count = 120
        updated_values = business_values(
            raw_business, location_key="t", scoring_config=get_scoring()
        )
        business, _ = await repo.upsert(updated_values)
        assert business.rating == 4.9
        assert business.review_count == 120

    async def test_first_discovered_is_never_overwritten(self, session, raw_business) -> None:
        """This column is how "new today" is computed; a re-run must not reset it."""
        repo = BusinessRepository(session)
        values = business_values(raw_business, location_key="t", scoring_config=get_scoring())
        business, _ = await repo.upsert(values)
        original = business.first_discovered_at

        await repo.upsert(values)
        refreshed = await repo.get(business.id)
        assert refreshed.first_discovered_at == original

    async def test_dedupe_key_blocks_a_second_provider(self, session, raw_business) -> None:
        """Same shop from a different provider must violate the unique index."""
        from sqlalchemy.exc import IntegrityError

        await _make_business(session, raw_business)
        await session.flush()

        raw_business.source = "osm"
        raw_business.source_id = "node/999"
        values = business_values(raw_business, location_key="t", scoring_config=get_scoring())

        with pytest.raises(IntegrityError):
            await BusinessRepository(session).upsert(values)
            await session.flush()

    async def test_lookup_by_phone(self, session, raw_business) -> None:
        await _make_business(session, raw_business)
        await session.flush()
        found = await BusinessRepository(session).find_probable_duplicate(
            phone_e164="+16269407551", website_domain=None, normalized_name="bobsplumbingdrain"
        )
        assert found is not None
        assert found.name.startswith("Bob's")

    async def test_chain_flag_is_computed(self, session, raw_business) -> None:
        raw_business.name = "Subway"
        raw_business.source_id = "chain-1"
        business = await _make_business(session, raw_business)
        assert business.is_chain is True


class TestAudits:
    async def test_audit_history_accumulates(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = AuditRepository(session)

        for score, status in [(20.0, WebsiteStatus.POOR), (35.0, WebsiteStatus.POOR)]:
            await repo.add(
                WebsiteAuditRecord(
                    business_id=business.id,
                    url="http://x.example",
                    outcome=AuditOutcome.OK,
                    score=score,
                    status=status,
                )
            )
        await session.flush()

        latest = await repo.latest_for_business(business.id)
        assert latest is not None
        assert latest.score == 35.0

    async def test_status_change_recorded_on_transition(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = AuditRepository(session)

        first = await repo.add(
            WebsiteAuditRecord(
                business_id=business.id,
                url="http://x.example",
                outcome=AuditOutcome.OK,
                score=30.0,
                status=WebsiteStatus.POOR,
            )
        )
        change = await repo.record_status_change(
            business_id=business.id,
            old=first,
            new_status=WebsiteStatus.BROKEN,
            new_score=5.0,
            new_url="http://x.example",
        )
        assert change is not None
        assert change.new_status == WebsiteStatus.BROKEN

    async def test_no_change_row_when_nothing_moved(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = AuditRepository(session)
        first = await repo.add(
            WebsiteAuditRecord(
                business_id=business.id,
                url="http://x.example",
                outcome=AuditOutcome.OK,
                score=30.0,
                status=WebsiteStatus.POOR,
            )
        )
        change = await repo.record_status_change(
            business_id=business.id,
            old=first,
            new_status=WebsiteStatus.POOR,
            new_score=31.0,
            new_url="http://x.example",
        )
        assert change is None


class TestLeads:
    async def test_create_and_rescore(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = LeadRepository(session)

        lead, created = await repo.upsert_score(
            business_id=business.id,
            score=80,
            website_status=WebsiteStatus.POOR,
            website_score=25.0,
            qualified=True,
            reason="bad site",
            components=[],
            adjustments=[],
        )
        assert created is True
        assert lead.status == LeadStatus.QUALIFIED

        lead, created = await repo.upsert_score(
            business_id=business.id,
            score=72,
            website_status=WebsiteStatus.POOR,
            website_score=30.0,
            qualified=True,
            reason="still bad",
            components=[],
            adjustments=[],
        )
        assert created is False
        assert lead.score == 72
        assert lead.previous_score == 80

    async def test_rescoring_preserves_sales_state(self, session, raw_business) -> None:
        """The whole point of splitting leads from businesses."""
        business = await _make_business(session, raw_business)
        repo = LeadRepository(session)

        lead, _ = await repo.upsert_score(
            business_id=business.id,
            score=80,
            website_status=WebsiteStatus.POOR,
            website_score=25.0,
            qualified=True,
            reason="x",
            components=[],
            adjustments=[],
        )
        await repo.update_status(
            lead.id,
            LeadStatus.CONTACTED,
            notes="Called Tuesday, asked for a callback",
            channel="phone",
        )
        await session.flush()

        await repo.upsert_score(
            business_id=business.id,
            score=79,
            website_status=WebsiteStatus.POOR,
            website_score=26.0,
            qualified=True,
            reason="y",
            components=[],
            adjustments=[],
        )
        refreshed = await repo.get_by_business(business.id)
        assert refreshed.status == LeadStatus.CONTACTED
        assert refreshed.notes == "Called Tuesday, asked for a callback"
        assert refreshed.contacted_at is not None

    async def test_score_history_grows(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = LeadRepository(session)
        for score in (60, 70, 80):
            await repo.upsert_score(
                business_id=business.id,
                score=score,
                website_status=WebsiteStatus.POOR,
                website_score=30.0,
                qualified=True,
                reason="x",
                components=[],
                adjustments=[],
            )
        await session.flush()

        # Counted with an explicit query: get_full does not eager-load history,
        # and touching a lazy relationship outside the loading context is what
        # produces MissingGreenlet under async SQLAlchemy.
        from sqlalchemy import func, select

        from leadgen.db.models import LeadScoreHistory

        lead = await repo.get_by_business(business.id)
        count = await session.scalar(
            select(func.count(LeadScoreHistory.id)).where(LeadScoreHistory.lead_id == lead.id)
        )
        assert count == 3

    async def test_priority_follows_score(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        lead, _ = await LeadRepository(session).upsert_score(
            business_id=business.id,
            score=92,
            website_status=WebsiteStatus.NONE,
            website_score=None,
            qualified=True,
            reason="x",
            components=[],
            adjustments=[],
        )
        assert lead.priority == 1

    async def test_search_filters(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = LeadRepository(session)
        await repo.upsert_score(
            business_id=business.id,
            score=88,
            website_status=WebsiteStatus.POOR,
            website_score=22.0,
            qualified=True,
            reason="x",
            components=[],
            adjustments=[],
        )
        await session.flush()

        # Assertions are scoped to the row this test created. Asserting that a
        # filter returns *nothing* globally would fail the moment the database
        # holds any other data, which it does in any real environment.
        async def finds_it(**filters) -> bool:
            rows = await repo.search(**filters, limit=1000)
            return any(row.business_id == business.id for row in rows)

        assert await finds_it(min_score=80, qualified_only=True)
        assert not await finds_it(min_score=95, qualified_only=True)
        assert await finds_it(cities=["Pasadena"])
        assert not await finds_it(cities=["Fresno"])
        assert await finds_it(niches=["plumbing"])
        assert await finds_it(query="bob")
        assert not await finds_it(query="zzz-no-such-business")

    async def test_count_matches_search(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = LeadRepository(session)
        await repo.upsert_score(
            business_id=business.id,
            score=88,
            website_status=WebsiteStatus.POOR,
            website_score=22.0,
            qualified=True,
            reason="x",
            components=[],
            adjustments=[],
        )
        await session.flush()
        rows = await repo.search(qualified_only=True, limit=1000)
        count = await repo.count_search(qualified_only=True)
        assert count == len(rows)

    async def test_follow_ups_due(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = LeadRepository(session)
        lead, _ = await repo.upsert_score(
            business_id=business.id,
            score=80,
            website_status=WebsiteStatus.POOR,
            website_score=25.0,
            qualified=True,
            reason="x",
            components=[],
            adjustments=[],
        )
        await repo.update_status(
            lead.id,
            LeadStatus.CONTACTED,
            follow_up_at=datetime.utcnow() - timedelta(days=1),
            channel="email",
        )
        await session.flush()
        assert len(await repo.due_follow_ups()) == 1


class TestCalledLeadsStopReappearing:
    """A business you already phoned must not come back tomorrow.

    This is the whole point of recording an outcome. If the daily list keeps
    surfacing someone who already said no, the operator stops trusting it and
    starts keeping their own spreadsheet, at which point the system is dead.
    """

    async def _lead(self, session, raw_business, name: str, score: int):
        # source_key is what the upsert matches on, and business_values has
        # already derived it from the fixture by the time these overrides
        # apply. Overriding source_id alone leaves both rows sharing a key, so
        # the second upsert returns the first business and the test quietly
        # asserts nothing. Override the key itself.
        business = await _make_business(
            session,
            raw_business,
            name=name,
            source_key=f"test:{name}",
            dedupe_key=name.lower().replace(" ", "-"),
            phone_e164=f"+1626555{score:04d}",
            street_address=f"{score} Test Street",
        )
        lead, _ = await LeadRepository(session).upsert_score(
            business_id=business.id,
            score=score,
            website_status=WebsiteStatus.POOR,
            website_score=20.0,
            qualified=True,
            reason="bad site",
            components=[],
            adjustments=[],
        )
        return lead

    async def test_contacted_lead_drops_out_of_the_daily_list(self, session, raw_business) -> None:
        from datetime import date, timedelta

        repo = LeadRepository(session)
        called = await self._lead(session, raw_business, "Called Roofing", 90)
        fresh = await self._lead(session, raw_business, "Untouched Roofing", 80)
        await session.flush()

        tomorrow = date.today() + timedelta(days=1)
        before = {lead.id for lead in await repo.top_for_date(tomorrow, limit=10)}
        assert {called.id, fresh.id} <= before

        await repo.update_status(called.id, LeadStatus.CONTACTED)
        await session.flush()

        after = {lead.id for lead in await repo.top_for_date(tomorrow, limit=10)}
        assert called.id not in after
        # The higher-scoring lead leaving must not take the rest with it.
        assert fresh.id in after

    async def test_do_not_contact_also_drops_out(self, session, raw_business) -> None:
        from datetime import date, timedelta

        repo = LeadRepository(session)
        refused = await self._lead(session, raw_business, "Refused Painting", 95)
        await session.flush()

        await repo.update_status(refused.id, LeadStatus.DO_NOT_CONTACT)
        await session.flush()

        tomorrow = date.today() + timedelta(days=1)
        assert refused.id not in {lead.id for lead in await repo.top_for_date(tomorrow, limit=10)}


class TestContactsAndSuppression:
    async def test_contacts_deduplicate(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        repo = ContactRepository(session)
        rows = [{"kind": "email", "value": "info@bobs.example", "source": "website"}]
        assert await repo.upsert_many(business.id, rows) == 1
        assert await repo.upsert_many(business.id, rows) == 0
        assert len(await repo.for_business(business.id)) == 1

    async def test_contacts_with_differing_keys(self, session, raw_business) -> None:
        """A real audit yields rows with different keys; the insert must cope.

        ``ContactFindings.as_contact_rows`` emits ``label`` and ``person_name``
        for emails, neither for phones, and no ``is_primary`` for socials.
        SQLAlchemy compiles a multi-row VALUES clause from the first row's keys
        and raises CompileError on any later row that omits one, so this
        combination crashed the pipeline in production while the homogeneous
        case above passed.
        """
        business = await _make_business(session, raw_business)
        repo = ContactRepository(session)
        rows = [
            {
                "kind": "email",
                "value": "owner@bobs.example",
                "source": "website",
                "label": "direct",
                "person_name": "Bob",
                "is_generic": False,
                "is_primary": True,
                "confidence": 0.85,
            },
            {"kind": "phone", "value": "+16265550101", "source": "website", "confidence": 0.8},
            {"kind": "social", "value": "https://fb.example/bobs", "label": "facebook"},
        ]
        assert await repo.upsert_many(business.id, rows) == 3

        stored = {c.kind: c for c in await repo.for_business(business.id)}
        assert stored["phone"].label is None
        assert stored["social"].person_name is None
        # Columns omitted by a row must fall back to their declared default,
        # not to NULL.
        assert stored["social"].source == "website"
        assert stored["phone"].is_primary is False
        assert stored["social"].confidence == pytest.approx(0.5)

    async def test_suppression_roundtrip(self, session) -> None:
        repo = SuppressionRepository(session)
        await repo.add("email", "stop@example.com", "opted out")
        await session.flush()
        active = await repo.active_values()
        assert "stop@example.com" in active["email"]

        await repo.remove("email", "stop@example.com")
        await session.flush()
        assert "stop@example.com" not in (await repo.active_values()).get("email", set())


class TestRunsAndAnalytics:
    async def test_run_lifecycle(self, session) -> None:
        from datetime import date

        repo = RunRepository(session)
        run = await repo.create(
            run_date=date.today(),
            trigger="test",
            location_key="test",
            niche_keys=["plumbing"],
            target_leads=50,
            config_snapshot={},
        )
        assert run.status == RunStatus.RUNNING

        await repo.finish(run.id, status=RunStatus.COMPLETED, discovered=10, qualified=4)
        await session.flush()
        latest = await repo.latest_for_date(date.today())
        assert latest.status == RunStatus.COMPLETED
        assert latest.discovered == 10
        assert latest.duration_seconds is not None

    async def test_overview_counts(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        await LeadRepository(session).upsert_score(
            business_id=business.id,
            score=88,
            website_status=WebsiteStatus.POOR,
            website_score=22.0,
            qualified=True,
            reason="x",
            components=[],
            adjustments=[],
        )
        await session.flush()

        overview = await AnalyticsRepository(session).overview()
        assert overview["total_businesses"] >= 1
        assert overview["qualified_leads"] >= 1
        assert overview["new_today"] >= 1

    async def test_grouped_analytics(self, session, raw_business) -> None:
        business = await _make_business(session, raw_business)
        await LeadRepository(session).upsert_score(
            business_id=business.id,
            score=88,
            website_status=WebsiteStatus.POOR,
            website_score=22.0,
            qualified=True,
            reason="x",
            components=[],
            adjustments=[],
        )
        await session.flush()

        repo = AnalyticsRepository(session)
        assert any(row["city"] == "Pasadena" for row in await repo.by_city())
        assert any(row["niche"] == "plumbing" for row in await repo.by_niche())
        assert await repo.by_website_status()
        assert len(await repo.conversion_funnel()) == 6
        assert len(await repo.score_distribution()) == 5


class TestBatchMarking:
    """Splitting the `mark` input, which is what the operator actually types.

    A calling session ends with several businesses to record at once, and one
    bad entry in the middle must not discard the good ones — nobody retypes
    the seven that worked.
    """

    def _split(self, raw: str) -> list[str]:
        return [part.strip() for part in raw.split(";") if part.strip()]

    def test_single_entry_is_unchanged(self) -> None:
        assert self._split("Valley Roofing") == ["Valley Roofing"]

    def test_several_entries_split_and_strip(self) -> None:
        assert self._split("A Roofing;  B Painting ;C Floors") == [
            "A Roofing",
            "B Painting",
            "C Floors",
        ]

    def test_commas_survive_because_names_contain_them(self) -> None:
        """ "Bob's Plumbing, Inc." is one business, not two."""
        assert self._split("Bob's Plumbing, Inc.") == ["Bob's Plumbing, Inc."]

    def test_trailing_separator_does_not_create_an_empty_term(self) -> None:
        assert self._split("A Roofing; B Painting;") == ["A Roofing", "B Painting"]

    def test_only_separators_yields_nothing(self) -> None:
        assert self._split(" ; ; ") == []
