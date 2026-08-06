"""Normalization tests.

These functions are the foundation of deduplication, so they are tested
exhaustively. A regression here silently creates duplicate leads, which is the
failure mode this whole system is supposed to prevent.
"""

from __future__ import annotations

import pytest

from leadgen.enrichment.normalize import (
    compute_dedupe_key,
    extract_domain,
    extract_emails,
    format_phone_display,
    guess_owner_name,
    is_generic_email,
    normalize_name,
    normalize_phone,
    normalize_state,
    normalize_street,
    normalize_url,
    normalize_zip,
    root_host,
)


class TestNormalizeName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Joe's Plumbing, Inc.", "joesplumbing"),
            ("JOE'S PLUMBING INC", "joesplumbing"),
            ("Joes Plumbing", "joesplumbing"),
            ("Joe's Plumbing LLC", "joesplumbing"),
            ("  Joe's   Plumbing  ", "joesplumbing"),
            ("Café Ole", "cafeole"),
            ("A & B Services", "ab"),
            ("", ""),
        ],
    )
    def test_collapses_variants(self, raw: str, expected: str) -> None:
        assert normalize_name(raw) == expected

    def test_none_is_empty(self) -> None:
        assert normalize_name(None) == ""

    def test_distinct_businesses_stay_distinct(self) -> None:
        assert normalize_name("Bob's Auto Repair") != normalize_name("Bob's Auto Body")


class TestNormalizeStreet:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("123 North Main Street", "123 N Main St"),
            ("456 West Oak Avenue", "456 W Oak Ave"),
            ("789 Sunset Boulevard, Suite 4", "789 Sunset Blvd Ste 4"),
            ("1 Park Drive", "1 Park Dr."),
        ],
    )
    def test_abbreviations_are_equivalent(self, a: str, b: str) -> None:
        assert normalize_street(a) == normalize_street(b)

    def test_different_numbers_differ(self) -> None:
        assert normalize_street("123 Main St") != normalize_street("125 Main St")


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw",
        ["(626) 940-7551", "626-940-7551", "626.940.7551", "+1 626 940 7551", "16269407551"],
    )
    def test_formats_converge_on_e164(self, raw: str) -> None:
        assert normalize_phone(raw) == "+16269407551"

    @pytest.mark.parametrize("raw", ["", "abc", "123", None, "000-000-0000"])
    def test_invalid_returns_none(self, raw: str | None) -> None:
        assert normalize_phone(raw) is None

    def test_display_format(self) -> None:
        assert format_phone_display("+16269407551") == "(626) 940-7551"


class TestDomains:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.example.com/page", "example.com"),
            ("http://example.com", "example.com"),
            ("example.com", "example.com"),
            ("https://shop.example.co.uk/x", "example.co.uk"),
            ("https://www.facebook.com/bobsplumbing", "facebook.com"),
        ],
    )
    def test_registrable_domain(self, url: str, expected: str) -> None:
        assert extract_domain(url) == expected

    def test_invalid_returns_none(self) -> None:
        assert extract_domain("not a url at all") is None
        assert extract_domain(None) is None

    def test_root_host_keeps_subdomain(self) -> None:
        assert root_host("https://shop.example.com/x") == "shop.example.com"
        assert root_host("https://www.example.com") == "example.com"

    def test_normalize_url_adds_scheme(self) -> None:
        assert normalize_url("example.com").startswith("https://")

    def test_normalize_url_strips_fragment(self) -> None:
        assert "#" not in (normalize_url("https://example.com/a#top") or "")


class TestDedupeKey:
    def test_same_business_different_formatting(self) -> None:
        a = compute_dedupe_key("Joe's Plumbing, Inc.", "123 North Main Street", "91101-1234")
        b = compute_dedupe_key("JOES PLUMBING", "123 N Main St", "91101")
        assert a == b

    def test_different_addresses_differ(self) -> None:
        a = compute_dedupe_key("Joe's Plumbing", "123 Main St", "91101")
        b = compute_dedupe_key("Joe's Plumbing", "456 Oak Ave", "91101")
        assert a != b

    def test_mobile_businesses_fall_back_to_phone(self) -> None:
        """Two service-area businesses with no address must not collide."""
        a = compute_dedupe_key("Mobile Detailing", None, "91101", phone_e164="+16261111111")
        b = compute_dedupe_key("Mobile Detailing", None, "91101", phone_e164="+16262222222")
        assert a != b

    def test_same_mobile_business_matches(self) -> None:
        a = compute_dedupe_key("Mobile Detail Co", None, "91101", phone_e164="+16261111111")
        b = compute_dedupe_key("Mobile Detail Co.", "", "91101", phone_e164="+16261111111")
        assert a == b


class TestEmails:
    def test_extracts_plain_addresses(self) -> None:
        # Not example.com: that is a reserved documentation domain and is
        # deliberately blocklisted, since it is never a real business.
        text = "Reach us at info@acme-plumbing.com or bob.smith@acme-plumbing.com today"
        assert set(extract_emails(text)) == {
            "info@acme-plumbing.com",
            "bob.smith@acme-plumbing.com",
        }

    def test_filters_asset_filenames(self) -> None:
        assert extract_emails("logo@2x.png sprite@3x.jpg") == []

    def test_filters_platform_noise(self) -> None:
        assert extract_emails("abc@sentry.wixpress.com") == []

    def test_generic_detection(self) -> None:
        assert is_generic_email("info@example.com")
        assert is_generic_email("Contact@Example.com")
        assert not is_generic_email("bob.smith@example.com")

    @pytest.mark.parametrize(
        "email,expected",
        [
            ("john.smith@acme.com", "John Smith"),
            ("mary_jones@acme.com", "Mary Jones"),
            ("info@acme.com", None),
            ("sales2024@acme.com", None),
            ("a@acme.com", None),
        ],
    )
    def test_owner_guess(self, email: str, expected: str | None) -> None:
        assert guess_owner_name(email) == expected


class TestMisc:
    def test_zip_truncates_plus_four(self) -> None:
        assert normalize_zip("91101-1234") == "91101"
        assert normalize_zip("91101") == "91101"
        assert normalize_zip("911") is None

    def test_state_names_and_codes(self) -> None:
        assert normalize_state("California") == "CA"
        assert normalize_state("ca") == "CA"
        assert normalize_state("CA") == "CA"
        assert normalize_state(None) is None
