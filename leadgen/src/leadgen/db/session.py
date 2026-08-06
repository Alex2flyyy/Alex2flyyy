"""Async engine, session factory, and unit-of-work helper."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from leadgen.config import get_settings
from leadgen.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        kwargs: dict = {"echo": s.db_echo, "future": True}

        if s.env == "test":
            # No pooling under test. asyncpg binds a connection to the event
            # loop that opened it, and the test suite runs a fresh loop per
            # test while sharing this module-level engine. A pooled connection
            # handed to a second loop raises "Task got Future attached to a
            # different loop"; NullPool opens and closes per checkout, so it
            # cannot happen.
            kwargs["poolclass"] = NullPool
        else:
            kwargs.update(
                pool_size=s.db_pool_size,
                max_overflow=s.db_max_overflow,
                pool_pre_ping=True,  # nightly runs idle long enough for the DB
                pool_recycle=1800,  # to drop connections; ping + recycle avoids it
            )

        _engine = create_async_engine(s.database_url, **kwargs)
        log.debug("engine.created", url=s.database_url.split("@")[-1], env=s.env)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,  # objects stay usable after commit
            autoflush=False,
        )
    return _session_factory


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
