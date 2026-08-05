"""Deduplication and geography tests."""

from __future__ import annotations

import math

import pytest

from leadgen.dedupe.matcher import InMemoryDeduper, build_identity, name_similarity
from leadgen.domain import RawBusiness
from leadgen.geo.grid import (
    Point,
    bbox_around,
    cells_needed,
    deduplicate_points,
    destination,
    haversine_km,
    haversine_miles,
    hex_tile,
    miles_to_km,
)


def make(name: str, **kwargs: object) -> RawBusiness:
    defaults: dict[str, object] = {
        "source": "google_places",
        "source_id": name.lower().replace(" ", "-"),
        "name": name,
        "street_address": "123 Main St",
        "city": "Pasadena",
        "state": "CA",
        "zip_code": "91101",
    }
    defaults.update(kwargs)
    return RawBusiness(**defaults)  # type: ignore[arg-type]


class TestNameSimilarity:
    def test_identical_after_normalization(self) -> None:
        assert name_similarity("Joe's Plumbing Inc", "JOES PLUMBING") == 1.0

    def test_related_names_score_mid(self) -> None:
        score = name_similarity("Bob's Auto Repair", "Bob's Auto Repair Shop")
        assert 0.4 < score < 1.0

    def test_unrelated_names_score_low(self) -> None:
        assert name_similarity("Joe's Plumbing", "Maria's Bakery") < 0.2

    def test_empty_is_zero(self) -> None:
        assert name_similarity(None, "x") == 0.0


class TestInMemoryDeduper:
    def test_exact_repeat_is_dropped(self) -> None:
        deduper = InMemoryDeduper()
        assert not deduper.seen(make("Joe's Plumbing"))
        assert deduper.seen(make("Joe's Plumbing"))
        assert deduper.duplicates_dropped == 1

    def test_formatting_variant_is_dropped(self) -> None:
        deduper = InMemoryDeduper()
        deduper.seen(
            make("Joe's Plumbing, Inc.", source_id="a", street_address="123 North Main Street")
        )
        assert deduper.seen(make("JOES PLUMBING", source_id="b", street_address="123 N Main St"))

    def test_shared_phone_and_similar_name_is_duplicate(self) -> None:
        deduper = InMemoryDeduper()
        deduper.seen(make("Joe's Plumbing", source_id="a", phone="(626) 940-7551"))
        assert deduper.seen(
            make(
                "Joe Plumbing Co",
                source_id="b",
                phone="626-940-7551",
                street_address="999 Other Rd",
                zip_code="91103",
            )
        )

    def test_shared_phone_with_unrelated_name_is_kept(self) -> None:
        """Answering services and shared lines are real; do not merge on phone alone."""
        deduper = InMemoryDeduper()
        deduper.seen(make("Joe's Plumbing", source_id="a", phone="(626) 940-7551"))
        assert not deduper.seen(
            make(
                "Maria's Bakery",
                source_id="b",
                phone="626-940-7551",
                street_address="999 Other Rd",
                zip_code="91103",
            )
        )

    def test_distinct_businesses_survive(self) -> None:
        deduper = InMemoryDeduper()
        businesses = [
            make("Joe's Plumbing", source_id="1"),
            make("Maria's Bakery", source_id="2", street_address="456 Oak Ave"),
            make("Tony's Roofing", source_id="3", street_address="789 Elm Dr"),
        ]
        kept = deduper.filter(businesses)
        assert len(kept) == 3

    def test_identity_fields(self) -> None:
        identity = build_identity(
            make("Joe's Plumbing", phone="(626) 940-7551", website="https://www.joesplumbing.com/x")
        )
        assert identity["source_key"] == "google_places:joe's-plumbing"
        assert identity["phone_e164"] == "+16269407551"
        assert identity["website_domain"] == "joesplumbing.com"
        assert len(identity["dedupe_key"] or "") == 40


class TestGeoMath:
    def test_haversine_known_distance(self) -> None:
        # Los Angeles to New York, ~3,936 km.
        la = Point(34.0522, -118.2437)
        ny = Point(40.7128, -74.0060)
        assert 3900 < haversine_km(la, ny) < 3980

    def test_haversine_zero(self) -> None:
        p = Point(34.0, -118.0)
        assert haversine_km(p, p) == pytest.approx(0.0, abs=1e-9)

    def test_miles_conversion(self) -> None:
        la = Point(34.0522, -118.2437)
        ny = Point(40.7128, -74.0060)
        assert haversine_miles(la, ny) == pytest.approx(haversine_km(la, ny) * 0.621371, rel=1e-6)

    def test_destination_round_trip(self) -> None:
        origin = Point(34.1478, -118.1445)
        moved = destination(origin, 90, 10.0)
        assert haversine_km(origin, moved) == pytest.approx(10.0, rel=0.01)

    def test_bbox_is_symmetric_around_center(self) -> None:
        center = Point(34.0, -118.0)
        box = bbox_around(center, 10.0)
        assert box.south < center.lat < box.north
        assert box.west < center.lng < box.east
        assert box.center.lat == pytest.approx(center.lat, abs=1e-6)


class TestHexTiling:
    def test_small_area_is_one_cell(self) -> None:
        cells = hex_tile(Point(34.0, -118.0), 2.0, 5.0)
        assert len(cells) == 1

    def test_larger_area_needs_more_cells(self) -> None:
        small = hex_tile(Point(34.0, -118.0), 10.0, 3.0)
        large = hex_tile(Point(34.0, -118.0), 30.0, 3.0)
        assert len(large) > len(small) * 4

    def test_cells_are_ordered_by_distance_from_center(self) -> None:
        center = Point(34.0, -118.0)
        cells = hex_tile(center, 25.0, 4.0)
        distances = [haversine_km(center, c) for c in cells]
        assert distances == sorted(distances)

    def test_coverage_has_no_gaps(self) -> None:
        """Every point in the area must be within one cell radius of some cell.

        This is the property that matters: a gap means businesses nobody ever
        sees, with no error to alert you.
        """
        center = Point(34.0, -118.0)
        area_km, cell_km = 20.0, 5.0
        cells = hex_tile(center, area_km, cell_km)

        # Sample the disc on a polar grid and check coverage at each point.
        uncovered = 0
        for ring in range(1, 9):
            radius = area_km * ring / 9
            for step in range(12):
                bearing = step * 30
                probe = destination(center, bearing, radius)
                if not any(haversine_km(probe, cell) <= cell_km for cell in cells):
                    uncovered += 1
        assert uncovered == 0, f"{uncovered} sampled points fall outside every search cell"

    def test_rejects_zero_radius(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            hex_tile(Point(34.0, -118.0), 10.0, 0.0)

    def test_cells_needed_matches_reality(self) -> None:
        estimate = cells_needed(20.0, 5.0)
        actual = len(hex_tile(Point(34.0, -118.0), 20.0, 5.0))
        # An estimate within 3x is good enough for cost projection.
        assert estimate / 3 <= actual <= estimate * 3

    def test_deduplicate_points_drops_neighbors(self) -> None:
        base = Point(34.0, -118.0)
        points = [base, destination(base, 0, 0.5), destination(base, 0, 20.0)]
        kept = deduplicate_points(points, min_separation_km=5.0)
        assert len(kept) == 2

    def test_miles_to_km(self) -> None:
        assert miles_to_km(25) == pytest.approx(40.2336, rel=1e-4)
        assert not math.isnan(miles_to_km(0))
