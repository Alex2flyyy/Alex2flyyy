"""Geographic math and search-cell tiling.

A provider search is a point plus a radius. Covering an *area* means choosing a
set of such points. Two facts drive the design:

1. **Hexagonal packing, not square.** Covering a plane with circles on a square
   lattice needs ~27% more circles than a hexagonal lattice for the same
   guaranteed coverage. At national scale that is a quarter of your API budget.

2. **Coverage spacing, not tangency.** Circles spaced exactly ``2r`` apart
   touch but leave gaps between them. Full coverage on a hex lattice needs
   spacing ``r * sqrt(3)``, which is what :func:`hex_tile` uses. Businesses
   falling in an uncovered gap are invisible to the entire system, and you'd
   never know.

All distances are computed on a spherical Earth. At the scale of a city or a
county the error versus a proper geodesic is well under a tenth of a percent —
irrelevant next to the fact that a "5 km radius" search is itself fuzzy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_KM = 6371.0088
MILES_PER_KM = 0.621371
KM_PER_MILE = 1.609344


@dataclass(slots=True, frozen=True)
class Point:
    lat: float
    lng: float


@dataclass(slots=True, frozen=True)
class BoundingBox:
    south: float
    west: float
    north: float
    east: float

    @property
    def center(self) -> Point:
        return Point((self.south + self.north) / 2, (self.west + self.east) / 2)

    def contains(self, point: Point) -> bool:
        return (
            self.south <= point.lat <= self.north and self.west <= point.lng <= self.east
        )

    def diagonal_km(self) -> float:
        return haversine_km(
            Point(self.south, self.west), Point(self.north, self.east)
        )


def haversine_km(a: Point, b: Point) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a.lat, a.lng, b.lat, b.lng))
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def haversine_miles(a: Point, b: Point) -> float:
    return haversine_km(a, b) * MILES_PER_KM


def destination(origin: Point, bearing_deg: float, distance_km: float) -> Point:
    """Point reached by travelling ``distance_km`` on ``bearing_deg`` from origin."""
    bearing = math.radians(bearing_deg)
    lat1 = math.radians(origin.lat)
    lng1 = math.radians(origin.lng)
    angular = distance_km / EARTH_RADIUS_KM

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lng2 = lng1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return Point(math.degrees(lat2), (math.degrees(lng2) + 540) % 360 - 180)


def bbox_around(center: Point, radius_km: float) -> BoundingBox:
    lat_delta = radius_km / 111.32
    # Longitude degrees shrink toward the poles; without the cosine correction a
    # bounding box in Alaska would be roughly twice as wide as intended.
    cos_lat = max(math.cos(math.radians(center.lat)), 1e-6)
    lng_delta = radius_km / (111.32 * cos_lat)
    return BoundingBox(
        south=max(center.lat - lat_delta, -90.0),
        west=max(center.lng - lng_delta, -180.0),
        north=min(center.lat + lat_delta, 90.0),
        east=min(center.lng + lng_delta, 180.0),
    )


def hex_tile(center: Point, area_radius_km: float, cell_radius_km: float) -> list[Point]:
    """Tile a disc with hexagonally packed search cells.

    Returns cell centers ordered by distance from ``center``, so a truncated
    list still covers the densest, most valuable core of the area rather than
    an arbitrary corner.
    """
    if cell_radius_km <= 0:
        raise ValueError("cell_radius_km must be positive")
    if area_radius_km <= cell_radius_km:
        return [center]

    spacing = cell_radius_km * math.sqrt(3)
    row_height = spacing * math.sqrt(3) / 2
    rows = int(math.ceil(area_radius_km / row_height)) + 1
    cols = int(math.ceil(area_radius_km / spacing)) + 1

    points: list[tuple[float, Point]] = []
    for row in range(-rows, rows + 1):
        north_km = row * row_height
        # Offset every other row by half a step — this is what makes the
        # lattice hexagonal rather than square.
        x_offset = (spacing / 2) if row % 2 else 0.0
        for col in range(-cols, cols + 1):
            east_km = col * spacing + x_offset
            planar_distance = math.hypot(north_km, east_km)
            # Keep cells whose disc still overlaps the target area.
            if planar_distance > area_radius_km + cell_radius_km * 0.5:
                continue
            candidate = center
            if north_km:
                candidate = destination(candidate, 0 if north_km > 0 else 180, abs(north_km))
            if east_km:
                candidate = destination(candidate, 90 if east_km > 0 else 270, abs(east_km))
            points.append((planar_distance, candidate))

    points.sort(key=lambda pair: pair[0])
    return [p for _, p in points]


def cells_needed(area_radius_km: float, cell_radius_km: float) -> int:
    """Estimate the cell count before generating them, for cost projection."""
    if area_radius_km <= cell_radius_km:
        return 1
    area = math.pi * area_radius_km**2
    # Hexagon inscribed around a circle of radius r has area 2*sqrt(3)*r^2.
    hex_area = 2 * math.sqrt(3) * cell_radius_km**2
    return max(1, int(math.ceil(area / hex_area)))


def deduplicate_points(points: list[Point], min_separation_km: float) -> list[Point]:
    """Drop points that sit almost on top of one another.

    City centroids from a gazetteer frequently overlap — Los Angeles, West
    Hollywood, and Beverly Hills are all within a few km. Searching all three
    at a 5 km radius returns largely the same businesses at triple the cost.
    """
    kept: list[Point] = []
    for point in points:
        if all(haversine_km(point, other) >= min_separation_km for other in kept):
            kept.append(point)
    return kept


def miles_to_km(miles: float) -> float:
    return miles * KM_PER_MILE


def km_to_miles(km: float) -> float:
    return km * MILES_PER_KM


def meters_to_km(meters: float) -> float:
    return meters / 1000.0
