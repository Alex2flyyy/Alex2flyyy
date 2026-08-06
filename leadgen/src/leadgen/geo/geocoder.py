"""Geocoding with a permanent database cache.

City centroids do not move, so a geocode result is cached forever. This matters
more than it sounds: without the cache, a nightly run over a dozen named
locations spends real money re-resolving the same twelve strings 365 times a
year, and adds a network round trip to startup every single night.

Google is the primary geocoder because the project already requires a Google
key for Places. Nominatim is the no-key fallback; its usage policy caps you at
1 request/second and requires a real contact in the User-Agent, both of which
are honored here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from leadgen.config import get_settings
from leadgen.geo.grid import BoundingBox, Point
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


@dataclass(slots=True)
class GeocodeResult:
    query: str
    found: bool
    lat: float | None = None
    lng: float | None = None
    viewport: BoundingBox | None = None
    formatted_address: str | None = None
    components: dict[str, str] | None = None
    provider: str = "google"

    @property
    def point(self) -> Point | None:
        if self.lat is None or self.lng is None:
            return None
        return Point(self.lat, self.lng)

    @property
    def approx_radius_km(self) -> float:
        """Half the viewport diagonal — a usable stand-in for "how big is this place".

        Falls back to 8 km, roughly a mid-sized suburb, when the provider gives
        no viewport.
        """
        if self.viewport is None:
            return 8.0
        return max(2.0, self.viewport.diagonal_km() / 2)


class Geocoder:
    """Cached geocoder. Pass a session to enable the database cache."""

    def __init__(self, fetcher: HttpFetcher, session: Any = None) -> None:
        self.fetcher = fetcher
        self.session = session
        self.settings = get_settings()
        self._memory: dict[str, GeocodeResult] = {}
        self.api_calls = 0

    async def geocode(self, query: str) -> GeocodeResult:
        key = query.strip().lower()
        if not key:
            return GeocodeResult(query=query, found=False)

        if key in self._memory:
            return self._memory[key]

        cached = await self._from_db(key)
        if cached is not None:
            self._memory[key] = cached
            return cached

        result = await self._lookup(query)
        self._memory[key] = result
        await self._to_db(key, result)
        return result

    async def _lookup(self, query: str) -> GeocodeResult:
        if self.settings.google_maps_api_key:
            result = await self._google(query)
            if result.found:
                return result
            log.debug("geocode.google_miss", query=query)
        return await self._nominatim(query)

    async def _google(self, query: str) -> GeocodeResult:
        self.api_calls += 1
        response = await self.fetcher.get(
            GOOGLE_GEOCODE_URL,
            params={"address": query, "key": self.settings.google_maps_api_key},
        )
        if not response.ok:
            log.warning("geocode.google_error", query=query, error=response.error)
            return GeocodeResult(query=query, found=False)

        try:
            data = json.loads(response.text) if response.text else {}
        except ValueError:
            return GeocodeResult(query=query, found=False)

        status = data.get("status")
        if status == "OVER_QUERY_LIMIT":
            log.error("geocode.quota_exceeded", query=query)
            return GeocodeResult(query=query, found=False)
        results = data.get("results") or []
        if status != "OK" or not results:
            return GeocodeResult(query=query, found=False)

        top = results[0]
        location = top["geometry"]["location"]
        viewport_raw = top["geometry"].get("viewport") or {}
        viewport = None
        if viewport_raw:
            viewport = BoundingBox(
                south=viewport_raw["southwest"]["lat"],
                west=viewport_raw["southwest"]["lng"],
                north=viewport_raw["northeast"]["lat"],
                east=viewport_raw["northeast"]["lng"],
            )

        components: dict[str, str] = {}
        for comp in top.get("address_components", []):
            for type_name in comp.get("types", []):
                components[type_name] = comp.get("short_name", "")

        return GeocodeResult(
            query=query,
            found=True,
            lat=location["lat"],
            lng=location["lng"],
            viewport=viewport,
            formatted_address=top.get("formatted_address"),
            components=components,
            provider="google",
        )

    async def _nominatim(self, query: str) -> GeocodeResult:
        self.api_calls += 1
        response = await self.fetcher.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": self.settings.user_agent},
        )
        if not response.ok:
            return GeocodeResult(query=query, found=False, provider="nominatim")
        try:
            data = json.loads(response.text)
        except ValueError:
            return GeocodeResult(query=query, found=False, provider="nominatim")
        if not data:
            return GeocodeResult(query=query, found=False, provider="nominatim")

        top = data[0]
        viewport = None
        if bb := top.get("boundingbox"):
            viewport = BoundingBox(
                south=float(bb[0]), north=float(bb[1]), west=float(bb[2]), east=float(bb[3])
            )
        address = top.get("address") or {}
        return GeocodeResult(
            query=query,
            found=True,
            lat=float(top["lat"]),
            lng=float(top["lon"]),
            viewport=viewport,
            formatted_address=top.get("display_name"),
            components={
                "locality": address.get("city") or address.get("town") or "",
                "administrative_area_level_2": address.get("county", ""),
                "administrative_area_level_1": address.get("state", ""),
                "postal_code": address.get("postcode", ""),
            },
            provider="nominatim",
        )

    async def _from_db(self, key: str) -> GeocodeResult | None:
        if self.session is None:
            return None
        from leadgen.db.repositories import GeoRepository

        row = await GeoRepository(self.session).cached_geocode(key)
        if row is None:
            return None
        viewport = None
        if row.viewport:
            viewport = BoundingBox(**row.viewport)
        return GeocodeResult(
            query=key,
            found=row.found,
            lat=float(row.lat) if row.lat is not None else None,
            lng=float(row.lng) if row.lng is not None else None,
            viewport=viewport,
            formatted_address=row.formatted_address,
            components=row.components or {},
            provider=row.provider,
        )

    async def _to_db(self, key: str, result: GeocodeResult) -> None:
        if self.session is None:
            return
        from leadgen.db.repositories import GeoRepository

        await GeoRepository(self.session).cache_geocode(
            key,
            {
                "lat": result.lat,
                "lng": result.lng,
                "viewport": {
                    "south": result.viewport.south,
                    "west": result.viewport.west,
                    "north": result.viewport.north,
                    "east": result.viewport.east,
                }
                if result.viewport
                else {},
                "formatted_address": result.formatted_address,
                "components": result.components or {},
                "provider": result.provider,
                "found": result.found,
            },
        )
