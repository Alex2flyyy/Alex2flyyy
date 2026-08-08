"""Scoring tests.

The scores drive which businesses get called, so the properties that must hold
are behavioral, not numeric: a bad site must score below a good one, an
unreachable business must not outrank a reachable one, and every score must
come with a human explanation.

Asserting exact numbers would make the tests fail every time a weight is tuned,
which is a change you want to be able to make freely.
"""

from __future__ import annotations

from leadgen.config import get_niches, get_scoring
from leadgen.domain import AuditOutcome, RawBusiness, WebsiteAudit, WebsiteStatus
from leadgen.scoring.lead_score import score_lead
from leadgen.scoring.website_score import score_website, summarize


def business(**overrides: object) -> RawBusiness:
    defaults = {
        "source": "google_places",
        "source_id": "x",
        "name": "Test Plumbing",
        "phone": "(626) 940-7551",
        "website": "http://test.example",
        "rating": 4.5,
        "review_count": 50,
        "business_status": "OPERATIONAL",
        "hours": {"Monday": "9-5"},
    }
    defaults.update(overrides)
    return RawBusiness(**defaults)  # type: ignore[arg-type]


class TestWebsiteScore:
    def test_bad_site_scores_low(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        assert bad_audit.score < 45
        assert bad_audit.status.is_opportunity

    def test_good_site_scores_high(self, good_audit: WebsiteAudit) -> None:
        score_website(good_audit)
        assert good_audit.score >= 75
        assert good_audit.status == WebsiteStatus.GOOD

    def test_good_beats_bad(self, good_audit: WebsiteAudit, bad_audit: WebsiteAudit) -> None:
        score_website(good_audit)
        score_website(bad_audit)
        assert good_audit.score > bad_audit.score + 30

    def test_score_is_always_in_range(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        assert 0 <= bad_audit.score <= 100

    def test_problems_are_human_readable(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        assert bad_audit.problems
        for problem in bad_audit.problems:
            # Sentences, not identifiers — these go in a sales email.
            assert len(problem) > 15
            assert problem[0].isupper() or problem[0].isdigit()
            assert "_" not in problem.split(" ")[0]

    def test_table_layout_is_outdated_not_merely_poor(self) -> None:
        audit = WebsiteAudit(url="http://x.example", outcome=AuditOutcome.OK)
        audit.uses_table_layout = True
        audit.has_viewport_meta = True
        audit.title = "A perfectly reasonable title here"
        audit.title_length = 33
        audit.word_count = 800
        score_website(audit)
        assert audit.status == WebsiteStatus.OUTDATED

    def test_unreachable_site_is_broken(self) -> None:
        audit = WebsiteAudit(url="http://gone.example", outcome=AuditOutcome.DNS_FAILURE)
        score_website(audit)
        assert audit.status == WebsiteStatus.BROKEN
        assert audit.score < 10
        assert "resolve" in audit.problems[0].lower()

    def test_robots_skip_does_not_fabricate_problems(self) -> None:
        """A site we chose not to crawl must not be reported as defective."""
        audit = WebsiteAudit(url="http://x.example", outcome=AuditOutcome.SKIPPED_ROBOTS)
        score_website(audit)
        assert audit.status == WebsiteStatus.UNKNOWN
        assert audit.problems == []

    def test_missing_dimension_does_not_penalize(self) -> None:
        """PageSpeed failing must not lower the score by itself."""
        with_psi = WebsiteAudit(url="http://a.example", outcome=AuditOutcome.OK)
        without_psi = WebsiteAudit(url="http://a.example", outcome=AuditOutcome.OK)
        for audit in (with_psi, without_psi):
            audit.uses_https = True
            audit.ssl_valid = True
            audit.has_viewport_meta = True
            audit.title = "A reasonable page title for a business"
            audit.title_length = 38
            audit.meta_description = "x" * 120
            audit.meta_description_length = 120
            audit.h1_count = 1
            audit.word_count = 700
            audit.has_contact_form = True
            audit.has_cta = True
            audit.has_click_to_call = True
        with_psi.psi_performance = 90.0
        with_psi.psi_seo = 90.0

        score_website(with_psi)
        score_website(without_psi)
        assert abs(with_psi.score - without_psi.score) < 12

    def test_summarize_is_a_sentence(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        text = summarize(bad_audit)
        assert text.startswith("Scores")
        assert len(text) > 40


class TestLeadScore:
    def test_no_website_outranks_decent_website(self, good_audit: WebsiteAudit) -> None:
        score_website(good_audit)
        niche = get_niches().get("plumbing")

        no_site = score_lead(business(website=None), None, niche, has_email=True)
        has_site = score_lead(business(), good_audit, niche, has_email=True)
        assert no_site.score > has_site.score

    def test_bad_website_outranks_good_website(
        self, bad_audit: WebsiteAudit, good_audit: WebsiteAudit
    ) -> None:
        score_website(bad_audit)
        score_website(good_audit)
        niche = get_niches().get("plumbing")

        bad = score_lead(business(), bad_audit, niche, has_email=True)
        good = score_lead(business(), good_audit, niche, has_email=True)
        assert bad.score > good.score

    def test_unreachable_business_is_penalized(self, bad_audit: WebsiteAudit) -> None:
        """The worst website in town is worthless if nobody answers."""
        score_website(bad_audit)
        niche = get_niches().get("plumbing")

        reachable = score_lead(business(), bad_audit, niche, has_email=True, has_phone=True)
        unreachable = score_lead(
            business(phone=None), bad_audit, niche, has_email=False, has_phone=False
        )
        assert unreachable.score < reachable.score
        assert any(key == "no_contact_channel_penalty" for key, _, _ in unreachable.adjustments)

    def test_permanently_closed_scores_zero(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        result = score_lead(
            business(business_status="CLOSED_PERMANENTLY"),
            bad_audit,
            get_niches().get("plumbing"),
        )
        assert result.score == 0
        assert not result.qualified

    def test_chain_is_penalized(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        niche = get_niches().get("restaurants")

        independent = score_lead(
            business(name="Maria's Taqueria"), bad_audit, niche, has_email=True
        )
        chain = score_lead(business(name="Subway"), bad_audit, niche, has_email=True)
        assert chain.score < independent.score

    def test_facebook_page_as_website_is_a_strong_lead(self) -> None:
        niche = get_niches().get("landscaping")
        result = score_lead(
            business(website="https://www.facebook.com/joeslandscaping"),
            None,
            niche,
            has_email=False,
        )
        assert result.qualified
        assert any(key == "facebook_page_as_website_bonus" for key, _, _ in result.adjustments)

    def test_score_bounds(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        for niche_key in get_niches().keys:
            result = score_lead(business(), bad_audit, get_niches().get(niche_key), has_email=True)
            assert 0 <= result.score <= 100

    def test_reasons_are_populated(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        result = score_lead(business(), bad_audit, get_niches().get("plumbing"), has_email=True)
        assert result.reasons
        assert all(len(r) > 10 for r in result.reasons)
        # No duplicates — the same reason twice reads as a bug to a salesperson.
        assert len({r.lower() for r in result.reasons}) == len(result.reasons)

    def test_components_cover_every_configured_weight(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        result = score_lead(business(), bad_audit, get_niches().get("plumbing"))
        keys = {c.key for c in result.components}
        assert keys == set(get_scoring().lead_score.weights)

    def test_explain_renders(self, bad_audit: WebsiteAudit) -> None:
        score_website(bad_audit)
        result = score_lead(business(), bad_audit, get_niches().get("plumbing"))
        text = result.explain()
        assert "Lead score:" in text
        assert "Web opportunity" in text


class TestConfigIntegrity:
    def test_website_weights_sum_to_one(self) -> None:
        assert abs(sum(get_scoring().website_score.weights.values()) - 1.0) < 1e-6

    def test_lead_weights_sum_to_one(self) -> None:
        assert abs(sum(get_scoring().lead_score.weights.values()) - 1.0) < 1e-6

    def test_every_niche_is_well_formed(self) -> None:
        for niche in get_niches().niches:
            assert niche.key and niche.label
            assert 0 <= niche.demand <= 1
            assert 0 <= niche.competition <= 1
            assert 0 <= niche.ticket <= 1
            # Every niche needs at least one way to be searched for.
            assert niche.keywords or niche.place_types

    def test_niche_keys_are_unique(self) -> None:
        keys = get_niches().keys
        assert len(keys) == len(set(keys))


class TestTimeBudget:
    """A spent wall-clock budget must stop work, not abort the run.

    Without this the pipeline can outlast the CI job's own timeout; the job is
    killed mid-audit, no report is written, and the run row stays "running"
    forever. Stopping early on purpose is strictly better.
    """

    def test_no_budget_never_stops(self) -> None:
        from leadgen.pipeline.daily import DailyPipeline, PipelineConfig

        pipeline = DailyPipeline(PipelineConfig())
        assert pipeline._out_of_time("audit") is False
        assert pipeline.result.errors == []

    def test_expired_budget_stops_and_records_once(self) -> None:
        import time

        from leadgen.pipeline.daily import DailyPipeline, PipelineConfig

        pipeline = DailyPipeline(PipelineConfig(time_budget_s=60))
        pipeline._deadline = time.monotonic() - 1

        assert pipeline._out_of_time("audit") is True
        assert pipeline._out_of_time("audit") is True
        # One message per stage, however many batches check it.
        assert len(pipeline.result.errors) == 1
        assert "time budget" in pipeline.result.errors[0]

    def test_unexpired_budget_allows_work(self) -> None:
        import time

        from leadgen.pipeline.daily import DailyPipeline, PipelineConfig

        pipeline = DailyPipeline(PipelineConfig(time_budget_s=60))
        pipeline._deadline = time.monotonic() + 30
        # Half the budget is gone, which is past discovery's share but well
        # inside the audit's; see TestDiscoveryBudgetShare.
        assert pipeline._out_of_time("audit") is False


class TestDiscoveryBudgetShare:
    """Discovery must not eat the whole budget.

    A run that discovered 166 businesses and audited none of them shipped an
    empty CSV: the businesses were saved, but nothing was scored, so the report
    had no rows. Discovery gets a share; the rest is reserved for the audit.
    """

    def test_discovery_stops_before_the_full_budget(self) -> None:
        import time

        from leadgen.pipeline.daily import DailyPipeline, PipelineConfig

        pipeline = DailyPipeline(PipelineConfig(time_budget_s=1000))
        # Half the budget spent: past discovery's 40% share, inside the total.
        pipeline._deadline = time.monotonic() + 500

        assert pipeline._out_of_time("discover") is True
        assert pipeline._out_of_time("audit") is False

    def test_discovery_runs_while_inside_its_share(self) -> None:
        import time

        from leadgen.pipeline.daily import DailyPipeline, PipelineConfig

        pipeline = DailyPipeline(PipelineConfig(time_budget_s=1000))
        pipeline._deadline = time.monotonic() + 900  # only 100s spent
        assert pipeline._out_of_time("discover") is False


class TestThinPresence:
    """The non-tech-savvy owner: callable, but nothing set up online.

    These are the best prospects for a web designer -- no incumbent agency, no
    in-house opinion, and an obvious gap to point at. But the bonus must not
    fire for a business nobody can reach, which is a dead end however bare its
    listing looks.
    """

    def _business(self, **overrides):
        from leadgen.domain import RawBusiness

        defaults = {
            "source": "osm",
            "source_id": "x1",
            "name": "Valley Builders",
            "phone": "(626) 555-0199",
            "website": None,
            "city": "Pasadena",
            "state": "CA",
            "review_count": 12,
            "rating": 4.4,
        }
        defaults.update(overrides)
        return RawBusiness(**defaults)

    def _keys(self, result):
        return {key for key, _, _ in result.adjustments}

    def test_established_but_no_web_presence_gets_bonus(self) -> None:
        from leadgen.scoring.lead_score import score_lead

        result = score_lead(self._business(), None, None)
        assert "thin_presence_bonus" in self._keys(result)

    def test_bare_listing_gets_the_unclaimed_variant(self) -> None:
        from leadgen.scoring.lead_score import score_lead

        result = score_lead(self._business(review_count=1), None, None)
        keys = self._keys(result)
        assert "unclaimed_listing_bonus" in keys
        assert "thin_presence_bonus" not in keys

    def test_unreachable_business_gets_no_bonus(self) -> None:
        from leadgen.scoring.lead_score import score_lead

        result = score_lead(self._business(phone=None), None, None)
        keys = self._keys(result)
        assert "thin_presence_bonus" not in keys
        assert "unclaimed_listing_bonus" not in keys
        assert "no_contact_channel_penalty" in keys

    def test_business_with_a_website_gets_no_bonus(self) -> None:
        from leadgen.scoring.lead_score import score_lead

        result = score_lead(self._business(website="http://valleybuilders.example"), None, None)
        assert "thin_presence_bonus" not in self._keys(result)

    def test_business_with_hours_listed_is_not_thin(self) -> None:
        """Hours mean somebody has been maintaining the listing."""
        from leadgen.scoring.lead_score import score_lead

        result = score_lead(self._business(hours={"mon": "8-5"}), None, None)
        assert "thin_presence_bonus" not in self._keys(result)

    def test_thin_presence_outranks_an_equivalent_managed_listing(self) -> None:
        from leadgen.scoring.lead_score import score_lead

        thin = score_lead(self._business(), None, None)
        managed = score_lead(self._business(hours={"mon": "8-5"}), None, None)
        assert thin.score > managed.score
