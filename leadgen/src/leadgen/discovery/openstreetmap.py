"""OpenStreetMap / Overpass provider.

Free, no API key, ODbL-licensed. Two reasons it earns a place next to Google
rather than being a poor-man's substitute:

1. **It is biased toward exactly the businesses you want.** OSM entries are
   community-contributed and frequently lack a ``website`` tag — often because
   the business genuinely has no website. That bias is a liability for a
   directory and an asset for prospecting.
2. **It costs nothing**, so it can run at national scale to build a candidate
   pool that Google is then used to enrich selectively.

Overpass is a shared volunteer resource. The public instance asks for modest
usage; this provider sends one query per cell, caps runtime server-side with
``[timeout:]``, and goes through the same per-host rate limiter as everything
else. If you scale past a few thousand queries a day, run your own Overpass
instance — do not just turn up the concurrency.
"""

from __future__ import annotations

import json
from typing import Any

from leadgen.config import Niche, get_settings
from leadgen.discovery.base import Provider
from leadgen.domain import RawBusiness, SearchCell
from leadgen.enrichment.normalize import normalize_state
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


class OpenStreetMapProvider(Provider):
    name = "osm"
    cost_per_call_usd = 0.0
    max_results_per_call = 500
    #: Give up on the provider after this many failures in a row. Overpass
    #: failing repeatedly means it is unreachable, not that one cell is odd.
    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, fetcher: HttpFetcher) -> None:
        super().__init__(fetcher)
        self.settings = get_settings()
        self._endpoint_index = 0
        self._consecutive_failures = 0

    async def available(self) -> bool:
        return not self._quota_exhausted

    async def search(
        self, cell: SearchCell, niche: Niche, *, limit: int = 100
    ) -> list[RawBusiness]:
        if not niche.osm_tags:
            return []

        query = self._build_query(cell, niche, limit)
        data = await self._execute(query)
        if not data:
            return []

        results: list[RawBusiness] = []
        for element in data.get("elements", []):
            business = self._parse(element, cell, niche)
            if business is not None:
                results.append(business)
            if len(results) >= limit:
                break
        return results

    def _build_query(self, cell: SearchCell, niche: Niche, limit: int) -> str:
        """Overpass QL for every configured tag, as nodes and ways.

        ``nwr`` covers nodes, ways, and relations in one clause. ``out center``
        gives a representative coordinate for ways/relations without dumping
        their full geometry, which would be orders of magnitude more data.
        """
        clauses = []
        for tag in niche.osm_tags:
            key, _, value = tag.partition("=")
            if value:
                clauses.append(
                    f'nwr["{key}"="{value}"](around:{cell.radius_m},{cell.lat},{cell.lng});'
                )
            else:
                clauses.append(f'nwr["{key}"](around:{cell.radius_m},{cell.lat},{cell.lng});')
        body = "\n  ".join(clauses)
        return f"[out:json][timeout:40];\n(\n  {body}\n);\nout center {limit};"

    async def _execute(self, query: str) -> dict[str, Any] | None:
        for _ in range(len(OVERPASS_ENDPOINTS)):
            endpoint = OVERPASS_ENDPOINTS[self._endpoint_index]
            self.api_calls += 1
            response = await self.fetcher.post_form(
                endpoint,
                {"data": query},
                headers={"User-Agent": self.settings.user_agent},
                timeout=90.0,  # Overpass is slow by design; its own timeout is 40s
            )

            if response.status_code in (429, 504):
                # Overpass sheds load aggressively. Rotate to the mirror rather
                # than retrying the instance that just told us it is busy.
                self._endpoint_index = (self._endpoint_index + 1) % len(OVERPASS_ENDPOINTS)
                log.warning("osm.endpoint_busy", endpoint=endpoint, status=response.status_code)
                continue
            if not response.ok:
                self.errors += 1
                self._consecutive_failures += 1
                log.warning(
                    "osm.request_failed",
                    status=response.status_code,
                    error=response.error,
                    consecutive=self._consecutive_failures,
                )
                # A reachability problem — a blocked egress, a firewall, a dead
                # mirror — fails identically on every cell. Without this the
                # provider retries all of them and burns the whole discovery
                # budget producing nothing, while the run still looks healthy.
                if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    self._mark_quota_exhausted()
                    log.error(
                        "osm.giving_up",
                        failures=self._consecutive_failures,
                        hint="Overpass is unreachable from this host; check egress "
                        "or set LEADGEN_GOOGLE_MAPS_API_KEY to use Google Places",
                    )
                return None
            try:
                data = json.loads(response.text)
            except ValueError:
                self.errors += 1
                self._consecutive_failures += 1
                return None
            self._consecutive_failures = 0
            return data
        self._mark_quota_exhausted()
        return None

    def _parse(self, element: dict[str, Any], cell: SearchCell, niche: Niche) -> RawBusiness | None:
        tags = element.get("tags") or {}
        name = tags.get("name")
        if not name:
            # An unnamed node is a map feature, not a business we can contact.
            return None

        center = element.get("center") or {}
        lat = element.get("lat") or center.get("lat")
        lng = element.get("lon") or center.get("lon")

        street = " ".join(
            part for part in (tags.get("addr:housenumber"), tags.get("addr:street")) if part
        ).strip()

        social = {}
        for key, platform in (
            ("contact:facebook", "facebook"),
            ("contact:instagram", "instagram"),
            ("contact:twitter", "twitter"),
        ):
            if value := tags.get(key):
                social[platform] = value

        return RawBusiness(
            source=self.name,
            source_id=f"{element.get('type', 'node')}/{element.get('id')}",
            name=name.strip(),
            phone=tags.get("phone") or tags.get("contact:phone"),
            website=tags.get("website") or tags.get("contact:website"),
            street_address=street or None,
            city=tags.get("addr:city") or cell.city,
            state=normalize_state(tags.get("addr:state")) or cell.state,
            zip_code=tags.get("addr:postcode") or cell.zip_code,
            lat=float(lat) if lat is not None else None,
            lng=float(lng) if lng is not None else None,
            google_maps_url=(
                f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
                if lat and lng
                else None
            ),
            categories=[niche.label],
            hours={"raw": tags["opening_hours"]} if tags.get("opening_hours") else {},
            description=tags.get("description"),
            niche_key=niche.key,
            search_cell=cell.key(),
            raw={
                "osm_tags": tags,
                "county": cell.county,
                "email": tags.get("email") or tags.get("contact:email"),
                "social": social,
            },
        )
