"""Alembic environment.

The database URL comes from ``LEADGEN_DATABASE_URL`` at runtime rather than
``alembic.ini``, so credentials are never committed and the same migrations run
against dev, staging, and production without editing a file.

Migrations run through the *synchronous* psycopg driver even though the
application uses asyncpg. Alembic's async support works, but schema changes are
a one-shot serial operation where async buys nothing and complicates failure
handling.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from leadgen.config import get_settings  # noqa: E402
from leadgen.db.base import Base  # noqa: E402
from leadgen.db import models  # noqa: E402,F401 - registers every table on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    if url := os.getenv("ALEMBIC_DATABASE_URL"):
        return url
    return get_settings().sync_database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``).

    Useful when a DBA must review and apply changes by hand.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # Keeps autogenerate from trying to drop tables created by other
            # tools that happen to share the database.
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
