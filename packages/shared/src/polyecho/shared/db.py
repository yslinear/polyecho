"""
polyecho.shared.db
==================
SQLAlchemy 2.0 async database layer.

Exports
-------
* :data:`engine`              — the global async engine (lazy-initialised).
* :data:`AsyncSessionLocal`   — session factory bound to ``engine``.
* :class:`Base`               — ``DeclarativeBase`` shared by all ORM models.
* :func:`get_engine`          — return (and cache) the async engine.
* :func:`session_scope`       — async context manager for a unit-of-work session.
* :func:`pg_listen`           — stub for Postgres ``LISTEN / NOTIFY`` helper.

Usage::

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Account

    async def get_accounts() -> list[Account]:
        async with session_scope() as session:
            result = await session.execute(select(Account))
            return result.scalars().all()
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from polyecho.shared.config import get_settings

# ---------------------------------------------------------------------------
# Declarative Base — all ORM models inherit from this.
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base shared by all PolyEcho ORM models."""


# ---------------------------------------------------------------------------
# Engine & session factory (module-level singletons, lazily created)
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the global :class:`AsyncEngine`, creating it on first call.

    The engine is configured with ``echo=False`` and ``pool_pre_ping=True``
    for robustness against stale connections.
    """
    global _engine  # noqa: PLW0603
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.db_url,
            echo=False,
            pool_pre_ping=True,
            # pool_size / max_overflow tuning can be added per-service later.
        )
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory  # noqa: PLW0603
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


def AsyncSessionLocal() -> AsyncSession:  # noqa: N802 — factory, named like a class
    """Return a new :class:`AsyncSession` from the lazily-built factory.

    Equivalent to ``sessionmaker()``: ``AsyncSessionLocal()`` yields a fresh
    session each call.  Prefer :func:`session_scope` for unit-of-work
    transactions; use this only when manual session lifecycle is required::

        async with AsyncSessionLocal() as session:
            ...

    The underlying engine and ``async_sessionmaker`` are created on first use
    so importing this module never opens a database connection.
    """
    return _get_session_factory()()


# ---------------------------------------------------------------------------
# session_scope — primary entry point for unit-of-work transactions
# ---------------------------------------------------------------------------


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that provides a transactional :class:`AsyncSession`.

    Commits on clean exit; rolls back on any exception and re-raises it.

    Example::

        async with session_scope() as session:
            session.add(Account(...))
        # committed here
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# LISTEN / NOTIFY helper stub (PRD §4.1)
# ---------------------------------------------------------------------------


async def pg_listen(
    channel: str,
    callback: Any,  # noqa: ANN401
    *,
    reconnect_delay: float = 5.0,
) -> None:
    """Subscribe to a Postgres ``NOTIFY`` channel and invoke *callback* on messages.

    This is a **stub** — the full implementation will use ``asyncpg`` directly
    (bypassing SQLAlchemy) to issue ``LISTEN <channel>`` and wait for
    notifications in a loop.  SQLAlchemy's async connection pool is not
    suitable for long-lived ``LISTEN`` connections; a dedicated ``asyncpg``
    connection is required.

    Args:
        channel:        Postgres channel name (e.g. ``"leader_trade"``)
        callback:       Async callable ``async def cb(connection, pid, channel, payload)``.
        reconnect_delay: Seconds to wait before reconnecting after a dropped
                        connection.

    Raises:
        NotImplementedError: Always — Phase 1 will implement this.

    .. note::
        PRD §4.1: ``trader-core`` and ``discovery-worker`` communicate via
        Postgres ``LISTEN / NOTIFY`` for sub-second latency without polling.
        The actual implementation will use ``asyncpg.connect()`` outside the
        SQLAlchemy pool and call ``await conn.add_listener(channel, callback)``.
    """
    # TODO(verify): implement with asyncpg.connect() + conn.add_listener()
    raise NotImplementedError(
        "pg_listen is a Phase 1 deliverable. "
        "Use asyncpg.connect() + conn.add_listener() outside the SA pool."
    )


async def create_all_tables() -> None:
    """Create all tables defined in :class:`Base` metadata (dev / test helper).

    In production use Drizzle migrations (``infra/migrations/``).
    """
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all_tables() -> None:
    """Drop all tables (test teardown helper — **never call in production**)."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
