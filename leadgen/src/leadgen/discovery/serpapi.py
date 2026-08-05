"""SerpAPI (Google Maps engine) provider.

A paid intermediary that returns Google Maps results without you scraping
Google yourself. Two situations justify the cost:

* You need fields the Places API does not expose in a search response.
* You want Maps *ranking* signals — where a business actually appears for a
  query — which is itself a strong "this business has an SEO problem" indicator.

Disabled unless ``LEADGEN_SERPAPI_KEY`` is set. Nothing depends on it.
"""

from __future__ import annotations

import json
from typing import Any

from leadgen.config import Niche, get_settings
from leadgen.discovery.base import Provider, QuotaExceeded
from leadgen.domain import RawBusiness, SearchCell
from leadgen.enrichment.normalize import normalize_state
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

SERPAPI_URL = "https://serpapi.com/search.json"


class SerpApiProvider(Provider):
    name = "serpapi"
    cost_per_call_usd = 0.01
    max_results_per_call = 20

    def __init__(self, fetcher: HttpFetcher) -> None:
        super().__init__(fetcher)
        self.api_key = get_settings().serpapi_key

    async def available(self) -> bool:
        return bool(self.api_key) and not self._quota_exhausted

    async def search(self, cell: SearchCell, niche: Niche, *, limit: int = 20) -> list[RawBusiness]:
        if not await self.available() or not niche.keywords:
            return []

        results: list[RawBusiness] = []
        seen: set[str] = set()

        for keyword in niche.keywords[:2]:
            if len(results) >= limit:
                break
            # SerpAPI's `ll` parameter takes a zoom level, not a radius. Zoom 14
            # is roughly a 3-5 km view, which matches our typical cell size.
            params = {
                "engine": "google_maps",
                "type": "search",
                "q": keyword,
                "ll": f"@{cell.lat},{cell.lng},14z",
                "hl": "en",
                "api_key": self.api_key,
            }
            self.api_calls += 1
            response = await self.fetcher.get(SERPAPI_URL, params=params)

            if response.status_code == 429:
                self._mark_quota_exhausted()
                raise QuotaExceeded("SerpAPI rate limit reached")
            if not response.ok:
                self.errors += 1
                log.warning("serpapi.failed", status=response.status_code, error=response.error)
                continue

            try:
                data = json.loads(response.text)
            except ValueError:
                self.errors += 1
                continue

            for index, item in enumerate(data.get("local_results", [])):
                place_id = item.get("place_id") or item.get("data_id")
                if not place_id or place_id in seen:
                    continue
                seen.add(place_id)
                business = self._parse(item, cell, niche, rank=index + 1, query=keyword)
                results.append(business)
                if len(results) >= limit:
                    break

        return results

    def _parse(
        self, item: dict[str, Any], cell: SearchCell, niche: Niche, *, rank: int, query: str
    ) -> RawBusiness:
        gps = item.get("gps_coordinates") or {}
        address = item.get("address") or ""
        city, state, zip_code = _split_address(address)

        return RawBusiness(
            source=self.name,
            source_id=str(item.get("place_id") or item.get("data_id")),
            name=(item.get("title") or "").strip(),
            phone=item.get("phone"),
            website=item.get("website"),
            street_address=address.split(",")[0].strip() or None,
            city=city or cell.city,
            state=normalize_state(state) or cell.state,
            zip_code=zip_code or cell.zip_code,
            lat=gps.get("latitude"),
            lng=gps.get("longitude"),
            google_maps_url=item.get("link"),
            categories=[item.get("type")] if item.get("type") else [niche.label],
            rating=item.get("rating"),
            review_count=item.get("reviews"),
            description=item.get("description"),
            business_status="OPERATIONAL",
            niche_key=niche.key,
            search_cell=cell.key(),
            # Map rank for the query is a genuine signal: a business sitting at
            # #17 for its own core keyword has a visibility problem worth
            # selling against.
            raw={"map_rank": rank, "map_query": query, "county": cell.county},
        )


def _split_address(address: str) -> tuple[str | None, str | None, str | None]:
    """Pull city/state/ZIP out of a display address string.

    SerpAPI returns addresses as one formatted line. This handles the common
    US shape ("123 Main St, Pasadena, CA 91101") and gives up gracefully on
    anything else rather than guessing wrong.
    """
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) < 2:
        return None, None, None
    tail = parts[-1].split()
    if len(tail) >= 2 and len(tail[0]) == 2 and tail[0].isalpha():
        return (parts[-2] if len(parts) >= 2 else None), tail[0], tail[1]
    if len(tail) == 1 and len(tail[0]) == 2:
        return (parts[-2] if len(parts) >= 2 else None), tail[0], None
    return parts[-2] if len(parts) >= 2 else None, None, None
