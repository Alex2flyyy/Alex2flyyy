"""Database URL normalization.

The point of these tests is that an operator can paste a hosted provider's
connection string verbatim into one environment variable and have both the
async app and the sync migration runner work. Every case below is a real
string shape a provider hands out.
"""

from __future__ import annotations

import pytest

from leadgen.config import normalize_database_url

# Exactly what Neon shows on its dashboard, including channel_binding.
NEON = (
    "postgresql://neondb_owner:npg_secret@ep-cool-fire-a4x.c-2.us-east-2."
    "aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)
SUPABASE = "postgres://postgres:pw@db.abcdef.supabase.co:5432/postgres?sslmode=require"
HEROKU = "postgres://u:p@ec2-1-2-3-4.compute-1.amazonaws.com:5432/dbname"
LOCAL = "postgresql://leadgen:leadgen@localhost:5432/leadgen"


class TestDriver:
    @pytest.mark.parametrize("url", [NEON, SUPABASE, HEROKU, LOCAL])
    def test_async_gets_asyncpg(self, url: str) -> None:
        assert normalize_database_url(url, is_async=True).startswith("postgresql+asyncpg://")

    @pytest.mark.parametrize("url", [NEON, SUPABASE, HEROKU, LOCAL])
    def test_sync_gets_psycopg(self, url: str) -> None:
        assert normalize_database_url(url, is_async=False).startswith("postgresql+psycopg://")

    def test_legacy_postgres_scheme_is_upgraded(self) -> None:
        assert normalize_database_url(HEROKU).startswith("postgresql+asyncpg://")

    def test_already_qualified_is_idempotent(self) -> None:
        once = normalize_database_url(NEON)
        assert normalize_database_url(once) == once


class TestTLS:
    def test_sslmode_becomes_ssl_for_asyncpg(self) -> None:
        """asyncpg raises TypeError on `sslmode`; it must be renamed."""
        result = normalize_database_url(NEON, is_async=True)
        assert "ssl=require" in result
        assert "sslmode" not in result

    def test_ssl_becomes_sslmode_for_psycopg(self) -> None:
        result = normalize_database_url("postgresql+asyncpg://u:p@h/db?ssl=require", is_async=False)
        assert "sslmode=require" in result
        assert "ssl=require" not in result

    def test_channel_binding_dropped_for_asyncpg(self) -> None:
        """asyncpg rejects this libpq-only parameter, and Neon always sends it."""
        assert "channel_binding" not in normalize_database_url(NEON, is_async=True)

    def test_channel_binding_kept_for_psycopg(self) -> None:
        assert "channel_binding" in normalize_database_url(NEON, is_async=False)

    def test_no_tls_params_invented_when_absent(self) -> None:
        """A local database without TLS must not suddenly require it."""
        for is_async in (True, False):
            result = normalize_database_url(LOCAL, is_async=is_async)
            assert "ssl" not in result


class TestPreservation:
    @pytest.mark.parametrize("url", [NEON, SUPABASE, HEROKU, LOCAL])
    def test_credentials_and_host_survive(self, url: str) -> None:
        for is_async in (True, False):
            result = normalize_database_url(url, is_async=is_async)
            # Everything between the scheme and the query must be untouched.
            assert url.split("://", 1)[1].split("?")[0] in result

    def test_port_preserved(self) -> None:
        assert ":5432" in normalize_database_url(SUPABASE)

    def test_empty_url_passes_through(self) -> None:
        assert normalize_database_url("") == ""

    def test_non_postgres_left_alone(self) -> None:
        sqlite = "sqlite+aiosqlite:///./test.db"
        assert normalize_database_url(sqlite) == sqlite


class TestSettingsIntegration:
    def test_settings_normalizes_on_load(self, monkeypatch) -> None:
        """The whole point: paste the provider string, get a working app."""
        from leadgen.config import Settings

        monkeypatch.setenv("LEADGEN_DATABASE_URL", NEON)
        settings = Settings()

        assert settings.database_url.startswith("postgresql+asyncpg://")
        assert "ssl=require" in settings.database_url
        assert "channel_binding" not in settings.database_url

        assert settings.sync_database_url.startswith("postgresql+psycopg://")
        assert "sslmode=require" in settings.sync_database_url
