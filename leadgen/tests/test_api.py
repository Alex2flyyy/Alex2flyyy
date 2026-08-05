"""API tests.

Exercised through FastAPI's TestClient against the real application and a real
database, so routing, dependency injection, serialization, and SQL are all
covered together. Mocking the repositories here would test the mocks.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from leadgen.config import get_scoring
from leadgen.db.repositories import BusinessRepository, LeadRepository
from leadgen.db.session import session_scope
from leadgen.domain import LeadStatus, WebsiteStatus
from leadgen.pipeline.stages import business_values
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture(scope="module")
def client() -> TestClient:
    from leadgen.api.app import create_app

    return TestClient(create_app())


@pytest.fixture
async def seeded_lead(raw_business):
    """A committed lead the API can actually read.

    Unlike the rolled-back `session` fixture, this commits: the TestClient runs
    the app's own sessions, which cannot see an uncommitted outer transaction.
    Cleaned up explicitly afterwards.
    """
    raw_business.source_id = "api-test-1"
    async with session_scope() as session:
        values = business_values(
            raw_business, location_key="test", scoring_config=get_scoring()
        )
        business, _ = await BusinessRepository(session).upsert(values)
        lead, _ = await LeadRepository(session).upsert_score(
            business_id=business.id,
            score=91,
            website_status=WebsiteStatus.POOR,
            website_score=24.0,
            qualified=True,
            reason="Site is not mobile friendly and has no SSL",
            components=[{"key": "web_opportunity", "label": "Web opportunity",
                         "raw": 76.0, "weight": 0.42, "contribution": 31.9,
                         "explanation": "Website scores 24/100"}],
            adjustments=[{"key": "ssl_missing_bonus", "delta": 6.0, "reason": "no valid SSL"}],
        )
        business_id, lead_id = business.id, lead.id

    yield {"lead_id": lead_id, "business_id": business_id}

    from sqlalchemy import delete

    from leadgen.db.models import Business

    async with session_scope() as session:
        await session.execute(delete(Business).where(Business.id == business_id))


class TestSystem:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"

    def test_openapi_schema(self, client: TestClient) -> None:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert "/api/leads" in response.json()["paths"]


class TestLeadEndpoints:
    def test_list_leads(self, client: TestClient, seeded_lead) -> None:
        # Searched by name rather than taking the first page: the ranking is by
        # score, so whether this lead appears in the top N depends on whatever
        # else happens to be in the database.
        response = client.get("/api/leads", params={"q": "bob's plumbing", "limit": 50})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1
        assert any(item["id"] == seeded_lead["lead_id"] for item in body["items"])

    def test_lead_detail(self, client: TestClient, seeded_lead) -> None:
        response = client.get(f"/api/leads/{seeded_lead['lead_id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["score"] == 91
        assert body["website_status_display"] == "Poor Website"
        assert body["business"]["name"].startswith("Bob's")
        # Phone must be reformatted for display, not returned as raw E.164.
        assert body["business"]["phone"] == "(626) 940-7551"
        assert body["score_components"]

    def test_missing_lead_is_404(self, client: TestClient) -> None:
        assert client.get("/api/leads/99999999").status_code == 404

    def test_score_filter(self, client: TestClient, seeded_lead) -> None:
        assert client.get("/api/leads", params={"min_score": 90}).json()["total"] >= 1
        high = client.get("/api/leads", params={"min_score": 99}).json()
        assert all(item["score"] >= 99 for item in high["items"])

    def test_invalid_score_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/leads", params={"min_score": 500}).status_code == 422

    def test_todays_leads(self, client: TestClient, seeded_lead) -> None:
        response = client.get("/api/leads/today", params={"limit": 5})
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_pagination_shape(self, client: TestClient, seeded_lead) -> None:
        body = client.get("/api/leads", params={"limit": 1, "offset": 0}).json()
        assert body["limit"] == 1
        assert len(body["items"]) <= 1


class TestStats:
    def test_overview(self, client: TestClient, seeded_lead) -> None:
        body = client.get("/api/stats/overview").json()
        assert body["total_businesses"] >= 1
        assert "avg_score" in body

    def test_full_stats_payload(self, client: TestClient, seeded_lead) -> None:
        body = client.get("/api/stats").json()
        for key in (
            "overview", "by_city", "by_niche", "by_website_status",
            "by_status", "score_distribution", "daily_trend", "funnel",
        ):
            assert key in body
        assert len(body["funnel"]) == 6

    def test_website_changes(self, client: TestClient) -> None:
        assert client.get("/api/stats/website-changes").status_code == 200


class TestWriteAuth:
    def test_update_without_key_is_allowed_in_dev(
        self, client: TestClient, seeded_lead
    ) -> None:
        """Development has no key configured, so writes are open by design."""
        response = client.patch(
            f"/api/leads/{seeded_lead['lead_id']}",
            json={"status": "contacted", "notes": "Called and left a voicemail",
                  "channel": "phone"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "contacted"

    def test_update_rejected_with_wrong_key(
        self, client: TestClient, seeded_lead, monkeypatch
    ) -> None:
        from leadgen.config import get_settings

        monkeypatch.setattr(get_settings(), "api_key", "the-real-key")
        response = client.patch(
            f"/api/leads/{seeded_lead['lead_id']}",
            json={"status": "contacted"},
            headers={"X-API-Key": "wrong"},
        )
        assert response.status_code == 401

    def test_update_accepted_with_right_key(
        self, client: TestClient, seeded_lead, monkeypatch
    ) -> None:
        from leadgen.config import get_settings

        monkeypatch.setattr(get_settings(), "api_key", "the-real-key")
        response = client.patch(
            f"/api/leads/{seeded_lead['lead_id']}",
            json={"status": "responded"},
            headers={"X-API-Key": "the-real-key"},
        )
        assert response.status_code == 200

    def test_suppression_marks_do_not_contact(
        self, client: TestClient, seeded_lead
    ) -> None:
        response = client.post(f"/api/leads/{seeded_lead['lead_id']}/suppress")
        assert response.status_code == 204

        detail = client.get(f"/api/leads/{seeded_lead['lead_id']}").json()
        assert detail["status"] == LeadStatus.DO_NOT_CONTACT.value


class TestExports:
    def test_destinations_listed(self, client: TestClient) -> None:
        body = client.get("/api/exports/destinations").json()
        assert "csv" in body["destinations"]
        assert "hubspot" in body["crm_formats"]

    def test_csv_export(self, client: TestClient, seeded_lead, tmp_path) -> None:
        os.environ["LEADGEN_EXPORT_DIR"] = str(tmp_path)
        response = client.post(
            "/api/exports", json={"destination": "csv", "filters": {"limit": 10}}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["output_path"]

    def test_unknown_destination_rejected(self, client: TestClient) -> None:
        response = client.post("/api/exports", json={"destination": "carrier-pigeon"})
        assert response.status_code == 400

    def test_unconfigured_destination_explains_itself(self, client: TestClient) -> None:
        response = client.post(
            "/api/exports", json={"destination": "notion", "filters": {"limit": 1}}
        )
        assert response.status_code == 400
        assert "NOTION" in response.json()["detail"].upper()


class TestDashboardPages:
    @pytest.mark.parametrize("path", ["/", "/leads", "/runs"])
    def test_pages_render(self, client: TestClient, seeded_lead, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "leadgen" in response.text

    def test_lead_detail_page(self, client: TestClient, seeded_lead) -> None:
        response = client.get(f"/leads/{seeded_lead['lead_id']}")
        assert response.status_code == 200
        assert "Score breakdown" in response.text

    def test_missing_lead_page_is_404(self, client: TestClient) -> None:
        assert client.get("/leads/99999999").status_code == 404


class TestRuns:
    def test_list_runs(self, client: TestClient) -> None:
        assert client.get("/api/runs").status_code == 200

    def test_dry_run_trigger_is_accepted(self, client: TestClient) -> None:
        response = client.post(
            "/api/runs/trigger",
            json={"location": "pasadena", "niches": ["plumbing"], "dry_run": True},
        )
        assert response.status_code == 202
        assert response.json()["accepted"] is True
