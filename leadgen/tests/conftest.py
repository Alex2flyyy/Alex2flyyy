"""Shared test fixtures.

Database tests run against a real Postgres because this schema leans on
Postgres-specific features — JSONB, array columns, partial indexes, and
``INSERT ... ON CONFLICT`` — that SQLite cannot emulate. Testing the deduper
against a database that does not enforce the unique constraints would test
nothing.

Set ``TEST_DATABASE_URL`` to point at a throwaway database. Without it, the
database-backed tests skip and the pure-logic tests still run, so
``pytest`` is useful on a laptop with no Postgres.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.pool import NullPool

from leadgen.config import reset_config_cache
from leadgen.domain import AuditOutcome, RawBusiness, WebsiteAudit

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set; skipping database tests"
)


@pytest.fixture(scope="session", autouse=True)
def _test_env() -> None:
    os.environ.setdefault("LEADGEN_ENV", "test")
    os.environ.setdefault("LEADGEN_LOG_LEVEL", "WARNING")
    os.environ.setdefault("LEADGEN_AI_ENABLED", "false")
    if TEST_DATABASE_URL:
        os.environ["LEADGEN_DATABASE_URL"] = TEST_DATABASE_URL
    reset_config_cache()


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    """A session whose work is always rolled back.

    Each test runs inside an outer transaction that is discarded at teardown,
    so tests cannot see each other's rows and never need cleanup code.

    The engine is built and disposed per test rather than reusing the
    application's cached one. pytest-asyncio gives each test a fresh event
    loop, and asyncpg connections are bound to the loop that created them —
    sharing a pooled engine across loops raises ``MissingGreenlet`` from
    whichever test happens to run second.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("no test database configured")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        await db.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
def raw_business() -> RawBusiness:
    return RawBusiness(
        source="google_places",
        source_id="ChIJtest12345",
        name="Bob's Plumbing & Drain, Inc.",
        phone="(626) 940-7551",
        website="http://bobsplumbing.example",
        street_address="123 North Main Street",
        city="Pasadena",
        state="CA",
        zip_code="91101",
        lat=34.1478,
        lng=-118.1445,
        rating=4.6,
        review_count=87,
        business_status="OPERATIONAL",
        hours={"Monday": "8 AM - 5 PM"},
        categories=["plumber"],
        niche_key="plumbing",
    )


@pytest.fixture
def bad_audit() -> WebsiteAudit:
    """An audit representing a genuinely poor site."""
    audit = WebsiteAudit(url="http://bobsplumbing.example", outcome=AuditOutcome.OK)
    audit.uses_https = False
    audit.ssl_valid = False
    audit.has_viewport_meta = False
    audit.uses_table_layout = True
    audit.title = "Home"
    audit.title_length = 4
    audit.meta_description = None
    audit.h1_count = 0
    audit.word_count = 60
    audit.response_time_ms = 4200
    audit.design_era = "2000s (table layout)"
    audit.copyright_year = 2008
    audit.has_contact_form = False
    audit.has_click_to_call = False
    audit.has_cta = False
    audit.total_images = 4
    audit.images_missing_alt = 4
    audit.horizontal_overflow = True
    audit.mobile_friendly = False
    return audit


@pytest.fixture
def good_audit() -> WebsiteAudit:
    audit = WebsiteAudit(url="https://modern.example", outcome=AuditOutcome.OK)
    audit.uses_https = True
    audit.ssl_valid = True
    audit.ssl_days_remaining = 200
    audit.has_viewport_meta = True
    audit.mobile_friendly = True
    audit.title = "Modern Plumbing Services in Pasadena, CA"
    audit.title_length = 40
    audit.meta_description = "x" * 140
    audit.meta_description_length = 140
    audit.h1_count = 1
    audit.word_count = 900
    audit.response_time_ms = 320
    audit.design_era = "current (2020s)"
    audit.copyright_year = 2026
    audit.has_contact_form = True
    audit.has_click_to_call = True
    audit.has_cta = True
    audit.has_structured_data = True
    audit.has_sitemap = True
    audit.has_favicon = True
    audit.has_open_graph = True
    audit.has_social_links = True
    audit.lang_attribute = "en"
    audit.total_images = 10
    audit.images_missing_alt = 0
    audit.psi_performance = 92.0
    audit.psi_seo = 96.0
    audit.psi_accessibility = 94.0
    audit.lcp_ms = 1400.0
    audit.cls = 0.02
    audit.page_weight_kb = 800.0
    audit.security_headers = {"strict-transport-security": "max-age=31536000"}
    return audit
