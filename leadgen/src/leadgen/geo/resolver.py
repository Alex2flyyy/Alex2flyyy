"""Turn a location target into an ordered list of search cells.

The ordering matters as much as the coverage. Cells come back sorted by
expected yield (population first, then proximity to the area's core), so a run
that stops early — because it hit its lead target, its API budget, or its time
budget — has spent its calls on the densest ground.

Large scopes are *seeded* from the ``geo_places`` gazetteer rather than
blind-tiled. Blind-tiling the continental US at a 6 km radius is roughly
250,000 searches; seeding from the 250 largest population centers is 250
searches that reach most of the country's businesses. When the gazetteer is
empty (fresh install), we fall back to geocoding the scope name and tiling its
viewport, with a loud warning, because that is strictly worse.
"""

from __future__ import annotations

from typing import Any

from leadgen.config import LocationTarget, get_locations
from leadgen.domain import SearchCell
from leadgen.geo.geocoder import Geocoder
from leadgen.geo.grid import (
    Point,
    deduplicate_points,
    haversine_km,
    hex_tile,
    meters_to_km,
    miles_to_km,
)
from leadgen.logging import get_logger

log = get_logger(__name__)


class LocationResolver:
    def __init__(self, geocoder: Geocoder, session: Any = None) -> None:
        self.geocoder = geocoder
        self.session = session

    async def resolve(self, target: LocationTarget | str) -> list[SearchCell]:
        if isinstance(target, str):
            target = get_locations().get(target)

        handler = {
            "radius": self._resolve_radius,
            "city": self._resolve_city,
            "zip": self._resolve_zips,
            "county": self._resolve_county,
            "state": self._resolve_state,
            "nation": self._resolve_nation,
        }[target.kind]

        cells = await handler(target)
        cells = cells[: target.max_cells]
        log.info(
            "geo.resolved",
            target=target.key or target.label,
            kind=target.kind,
            cells=len(cells),
            cell_radius_m=target.cell_radius_m,
        )
        return cells

    # -- radius ----------------------------------------------------------

    async def _resolve_radius(self, target: LocationTarget) -> list[SearchCell]:
        if target.lat is not None and target.lng is not None:
            center = Point(target.lat, target.lng)
            label_base = f"{target.lat:.4f},{target.lng:.4f}"
            components: dict[str, str] = {}
        else:
            result = await self.geocoder.geocode(target.center or "")
            if not result.found or result.point is None:
                log.error("geo.geocode_failed", target=target.key, center=target.center)
                return []
            center = result.point
            label_base = target.center or target.label
            components = result.components or {}

        radius_km = miles_to_km(target.radius_miles or 10)
        cell_km = meters_to_km(target.cell_radius_m)
        points = hex_tile(center, radius_km, cell_km)

        return [
            SearchCell(
                lat=p.lat,
                lng=p.lng,
                radius_m=target.cell_radius_m,
                label=f"{label_base} +{haversine_km(center, p):.0f}km",
                city=components.get("locality"),
                county=components.get("administrative_area_level_2"),
                state=components.get("administrative_area_level_1"),
            )
            for p in points
        ]

    # -- city ------------------------------------------------------------

    async def _resolve_city(self, target: LocationTarget) -> list[SearchCell]:
        place = await self._lookup_place(target.city or "", target.state or "")
        if place is not None:
            center = Point(float(place.lat), float(place.lng))
            # Rough radius from population: a 50k-person city is ~5 km across,
            # and area scales roughly with population for US municipalities.
            area_km = max(3.0, (place.population / 50_000) ** 0.5 * 5.0)
            county = place.county
        else:
            result = await self.geocoder.geocode(f"{target.city}, {target.state}")
            if not result.found or result.point is None:
                log.error("geo.city_not_found", city=target.city, state=target.state)
                return []
            center = result.point
            area_km = result.approx_radius_km
            county = (result.components or {}).get("administrative_area_level_2")

        cell_km = meters_to_km(target.cell_radius_m)
        points = hex_tile(center, area_km, cell_km)
        return [
            SearchCell(
                lat=p.lat,
                lng=p.lng,
                radius_m=target.cell_radius_m,
                label=f"{target.city}, {target.state}",
                city=target.city,
                county=county,
                state=target.state,
            )
            for p in points
        ]

    # -- zip -------------------------------------------------------------

    async def _resolve_zips(self, target: LocationTarget) -> list[SearchCell]:
        cells: list[SearchCell] = []
        for zip_code in target.zips:
            result = await self.geocoder.geocode(f"{zip_code}, {target.country_code}")
            if not result.found or result.point is None:
                log.warning("geo.zip_not_found", zip=zip_code)
                continue
            components = result.components or {}
            # A ZIP is small; usually one cell covers it. Tile only when the
            # configured cell radius is smaller than the ZIP's own footprint.
            points = hex_tile(
                result.point,
                min(result.approx_radius_km, 8.0),
                meters_to_km(target.cell_radius_m),
            )
            cells.extend(
                SearchCell(
                    lat=p.lat,
                    lng=p.lng,
                    radius_m=target.cell_radius_m,
                    label=f"ZIP {zip_code}",
                    city=components.get("locality"),
                    county=components.get("administrative_area_level_2"),
                    state=components.get("administrative_area_level_1"),
                    zip_code=zip_code,
                )
                for p in points
            )
        return cells

    # -- county / state / nation ----------------------------------------

    async def _resolve_county(self, target: LocationTarget) -> list[SearchCell]:
        places = await self._places(
            "county",
            county=target.county,
            state=target.state,
            min_population=target.min_population,
            limit=target.max_cells * 2,
        )
        if not places:
            return await self._fallback_area(target, f"{target.county} County, {target.state}")
        return self._cells_from_places(places, target, f"{target.county} County")

    async def _resolve_state(self, target: LocationTarget) -> list[SearchCell]:
        places = await self._places(
            "state",
            state=target.state,
            min_population=target.min_population,
            limit=target.max_cells * 2,
        )
        if not places:
            return await self._fallback_area(target, f"{target.state}, USA")
        return self._cells_from_places(places, target, target.state or "")

    async def _resolve_nation(self, target: LocationTarget) -> list[SearchCell]:
        places = await self._places(
            "nation",
            country_code=target.country_code,
            min_population=target.min_population,
            limit=target.max_cells * 2,
        )
        if not places:
            log.error(
                "geo.gazetteer_empty",
                hint="run `leadgen geo import <csv>` to load US places; "
                "nation-scale targets cannot be resolved without it",
            )
            return []
        return self._cells_from_places(places, target, target.country_code)

    def _cells_from_places(
        self, places: list[Any], target: LocationTarget, scope_label: str
    ) -> list[SearchCell]:
        cell_km = meters_to_km(target.cell_radius_m)

        # Adjacent municipalities overlap heavily. Require centers to be at
        # least one cell radius apart before spending a search on each.
        points = [Point(float(p.lat), float(p.lng)) for p in places]
        kept_points = {
            (p.lat, p.lng) for p in deduplicate_points(points, min_separation_km=cell_km)
        }

        cells: list[SearchCell] = []
        for place in places:
            key = (float(place.lat), float(place.lng))
            if key not in kept_points:
                continue
            cells.append(
                SearchCell(
                    lat=float(place.lat),
                    lng=float(place.lng),
                    radius_m=target.cell_radius_m,
                    label=f"{place.name}, {place.state} ({scope_label})",
                    city=place.name,
                    county=place.county,
                    state=place.state,
                    population=place.population,
                )
            )
        # Population order is the whole point of seeding: search where the
        # businesses are before searching where they aren't.
        cells.sort(key=lambda c: -(c.population or 0))
        return cells

    async def _fallback_area(self, target: LocationTarget, query: str) -> list[SearchCell]:
        log.warning(
            "geo.gazetteer_miss",
            query=query,
            hint="falling back to viewport tiling; import a gazetteer for better coverage",
        )
        result = await self.geocoder.geocode(query)
        if not result.found or result.point is None:
            return []
        points = hex_tile(result.point, result.approx_radius_km, meters_to_km(target.cell_radius_m))
        return [
            SearchCell(
                lat=p.lat,
                lng=p.lng,
                radius_m=target.cell_radius_m,
                label=query,
                county=target.county,
                state=target.state,
            )
            for p in points
        ]

    # -- gazetteer access ------------------------------------------------

    async def _places(self, scope: str, **kwargs: Any) -> list[Any]:
        if self.session is None:
            return []
        from leadgen.db.repositories import GeoRepository

        repo = GeoRepository(self.session)
        limit = kwargs.pop("limit", 200)
        min_population = kwargs.pop("min_population", 0)
        if scope == "county":
            rows = await repo.places_for_county(
                kwargs["county"], kwargs["state"], min_population=min_population, limit=limit
            )
        elif scope == "state":
            rows = await repo.places_for_state(
                kwargs["state"], min_population=min_population, limit=limit
            )
        else:
            rows = await repo.places_for_country(
                kwargs.get("country_code", "US"), min_population=min_population, limit=limit
            )
        return list(rows)

    async def _lookup_place(self, city: str, state: str) -> Any:
        if self.session is None or not city or not state:
            return None
        from leadgen.db.repositories import GeoRepository

        return await GeoRepository(self.session).find_place(city, state)


async def estimate_cost(
    cells: list[SearchCell], niches: int, cost_per_search_usd: float = 0.032
) -> dict[str, Any]:
    """Project the provider spend for a planned run.

    Google Places Nearby Search (New) with the Pro SKU is roughly $32 per 1,000
    calls at the time of writing. Confirm current pricing before relying on
    this — it exists so a "whole United States" run cannot surprise you.
    """
    searches = len(cells) * max(niches, 1)
    return {
        "cells": len(cells),
        "niches": niches,
        "searches": searches,
        "estimated_usd": round(searches * cost_per_search_usd, 2),
        "note": "verify current Places API pricing; excludes Place Details calls",
    }
