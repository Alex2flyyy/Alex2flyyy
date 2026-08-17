"""Regression tests for the Google Places provider.

These cover the failure that made construction — the niche the system is
actually pointed at — return zero businesses from Google every run while the
run still reported success. Google rejected the `general_contractor` type as
unsupported; the empty result then failed to trigger the keyword fallback, so
the niche was silently dead.
"""

from __future__ import annotations

import json

import pytest

from leadgen.config import Niche
from leadgen.discovery.google_places import GooglePlacesProvider
from leadgen.domain import SearchCell
from leadgen.http import FetchResult

UNSUPPORTED_TYPE_BODY = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "Unsupported types: general_contractor.",
            "status": "INVALID_ARGUMENT",
        }
    }
)


def _place(place_id: str, name: str) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": name},
        "formattedAddress": "1 Main St, Pasadena, CA 91101",
        "location": {"latitude": 34.14, "longitude": -118.14},
    }


class RecordingFetcher:
    """Replays queued responses and remembers every payload it was given."""

    def __init__(self, responses: list[FetchResult]) -> None:
        self._responses = responses
        self.payloads: list[dict] = []

    async def post_json(self, url: str, payload: dict, headers: dict) -> FetchResult:
        self.payloads.append({"url": url, **payload})
        if self._responses:
            return self._responses.pop(0)
        return FetchResult(url=url, final_url=url, status_code=200, text=json.dumps({}))


def _ok(places: list[dict]) -> FetchResult:
    return FetchResult(
        url="x", final_url="x", status_code=200, text=json.dumps({"places": places})
    )


def _rejected() -> FetchResult:
    return FetchResult(
        url="x", final_url="x", status_code=400, text=UNSUPPORTED_TYPE_BODY
    )


def _provider(fetcher: RecordingFetcher) -> GooglePlacesProvider:
    provider = GooglePlacesProvider(fetcher)  # type: ignore[arg-type]
    provider.api_key = "test-key"
    return provider


@pytest.fixture
def cell() -> SearchCell:
    return SearchCell(lat=34.14, lng=-118.14, radius_m=4000, label="Pasadena")


@pytest.fixture
def construction() -> Niche:
    return Niche(
        key="construction",
        label="Construction",
        place_types=["general_contractor"],
        keywords=["general contractor", "construction company"],
        osm_tags=[],
    )


@pytest.mark.asyncio
class TestUnsupportedPlaceTypes:
    async def test_rejected_type_falls_back_to_keyword_search(self, cell, construction):
        """The whole niche used to return nothing. It must still find businesses."""
        fetcher = RecordingFetcher(
            [_rejected(), _ok([_place("a", "Alvarez Construction")])]
        )
        provider = _provider(fetcher)

        results = await provider.search(cell, construction, limit=20)

        assert [r.name for r in results] == ["Alvarez Construction"]
        assert any("textQuery" in p for p in fetcher.payloads), (
            "a nearby search that returned nothing must fall through to keywords"
        )

    async def test_rejected_type_is_not_sent_again(self, cell, construction):
        """45 cells x 5 niches means the same doomed call 45 times per run."""
        fetcher = RecordingFetcher([_rejected(), _ok([]), _ok([])])
        provider = _provider(fetcher)

        await provider.search(cell, construction, limit=20)
        assert provider._rejected_types == {"general_contractor"}

        before = len(fetcher.payloads)
        await provider.search(cell, construction, limit=20)
        resent = [
            p
            for p in fetcher.payloads[before:]
            if "general_contractor" in p.get("includedTypes", [])
        ]
        assert resent == []

    async def test_valid_types_survive_an_unrelated_rejection(self, cell):
        provider = _provider(RecordingFetcher([]))
        provider._remember_rejected_types(UNSUPPORTED_TYPE_BODY)
        assert "roofing_contractor" not in provider._rejected_types

    async def test_non_type_error_body_is_ignored(self, cell):
        provider = _provider(RecordingFetcher([]))
        provider._remember_rejected_types(
            json.dumps({"error": {"message": "Request had invalid authentication."}})
        )
        provider._remember_rejected_types("not json at all")
        assert provider._rejected_types == set()


@pytest.mark.asyncio
class TestKeywordTopUp:
    async def test_full_nearby_page_still_tops_up_with_keywords(self, cell):
        """A full page means the cell almost certainly held more than 20."""
        niche = Niche(
            key="roofing",
            label="Roofing",
            place_types=["roofing_contractor"],
            keywords=["roofing contractor"],
            osm_tags=[],
        )
        fetcher = RecordingFetcher(
            [
                _ok([_place(f"n{i}", f"Roofer {i}") for i in range(20)]),
                _ok([_place("t1", "Keyword Roofing")]),
            ]
        )
        results = await _provider(fetcher).search(cell, niche, limit=25)

        assert len(results) == 21
        assert "Keyword Roofing" in [r.name for r in results]
