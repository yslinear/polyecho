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
* :func:`pg_listen`           — Postgres ``LISTEN / NOTIFY`` callback loop.
* :func:`pg_listen_iter`      — ``async with`` / ``async for`` notification
                                stream (PRD §4.1 instant sync).

Usage::

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Account

    async def get_accounts() -> list[Account]:
        async with session_scope() as session:
            result = await session.execute(select(Account))
            return result.scalars().all()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from polyecho.shared.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    import asyncpg  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

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
# LISTEN / NOTIFY helper (PRD §4.1 — instant sync without polling)
# ---------------------------------------------------------------------------
#
# SQLAlchemy's async pool is unsuitable for a long-lived ``LISTEN`` connection
# (pooled connections are recycled / pre-pinged and may be handed to other
# callers), so this uses a **dedicated** ``asyncpg`` connection created
# *outside* the SQLAlchemy engine, exactly as PRD §4.1 prescribes.


def _asyncpg_dsn(db_url: str | None = None) -> str:
    """Return a libpq DSN usable by ``asyncpg.connect``.

    ``Settings.db_url`` is a SQLAlchemy URL (``postgresql+asyncpg://…``).
    asyncpg wants a plain ``postgresql://…`` DSN, so the ``+asyncpg`` (or any
    other ``+driver``) suffix on the scheme is stripped.

    Args:
        db_url: Override URL; defaults to ``get_settings().db_url``.

    Returns:
        A ``postgresql://…`` DSN string.
    """
    url = db_url if db_url is not None else get_settings().db_url
    scheme, sep, rest = url.partition("://")
    if sep and "+" in scheme:
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}{sep}{rest}" if sep else url


async def _connect_asyncpg(dsn: str) -> asyncpg.Connection[Any]:
    """Open a single dedicated ``asyncpg`` connection (lazy import).

    ``asyncpg`` is imported lazily so importing :mod:`polyecho.shared.db`
    never requires the driver nor opens a socket (mirrors the engine
    laziness).  Isolated in one function so tests can monkeypatch it.
    """
    import asyncpg  # local import — keeps module import side-effect free

    conn: asyncpg.Connection[Any] = await asyncpg.connect(dsn)
    return conn


@dataclass(frozen=True)
class Notification:
    """One ``NOTIFY`` message yielded by :func:`pg_listen_iter`.

    Attributes:
        pid:     Server PID of the notifying backend.
        channel: The channel the notification arrived on.
        payload: The (possibly empty) string payload.
    """

    pid: int
    channel: str
    payload: str


@asynccontextmanager
async def pg_listen_iter(
    channel: str,
    *,
    db_url: str | None = None,
) -> AsyncGenerator[AsyncGenerator[Notification, None], None]:
    """``async with`` a dedicated ``LISTEN`` connection, ``async for`` notes.

    Opens one dedicated ``asyncpg`` connection (outside the SQLAlchemy pool),
    issues ``LISTEN <channel>``, and yields an async generator of
    :class:`Notification`.  The connection's ``add_listener`` callback feeds
    an :class:`asyncio.Queue`; iterating the generator drains the queue.  On
    ``async with`` exit (normal *or* cancellation) the listener is removed and
    the connection closed cleanly.

    Example::

        async with pg_listen_iter("leader_trade") as notes:
            async for n in notes:
                handle(n.payload)

    Args:
        channel: Postgres channel name (e.g. ``"leader_trade"``).
        db_url:  Optional SQLAlchemy/libpq URL override (tests inject this).

    Yields:
        An ``AsyncGenerator[Notification, None]``.
    """
    conn = await _connect_asyncpg(_asyncpg_dsn(db_url))
    queue: asyncio.Queue[Notification] = asyncio.Queue()

    def _on_notify(
        _conn: object, pid: int, chan: str, payload: str
    ) -> None:
        # asyncpg invokes this from the connection's read loop; only a
        # non-blocking put_nowait is safe here.
        queue.put_nowait(Notification(pid=pid, channel=chan, payload=payload))

    async def _stream() -> AsyncGenerator[Notification, None]:
        while True:
            yield await queue.get()

    await conn.add_listener(channel, _on_notify)
    gen = _stream()
    try:
        yield gen
    finally:
        await gen.aclose()
        with contextlib.suppress(Exception):
            await conn.remove_listener(channel, _on_notify)
        with contextlib.suppress(Exception):
            await conn.close()


async def _listen_once(
    dsn: str, channel: str, callback: Any  # noqa: ANN401
) -> None:
    """Run one dedicated ``LISTEN`` connection until it drops or is cancelled.

    Factored out of :func:`pg_listen` so the termination-listener closure
    captures a *fresh* per-call ``terminated`` Event (no loop-variable
    binding — ruff B023).  Returns normally when the connection drops (the
    caller reconnects); on cancellation it cleans up then re-raises;
    connect/other failures propagate to the caller's reconnect handler.
    """
    conn: asyncpg.Connection[Any] | None = None
    # Set by asyncpg's termination listener the moment the dedicated
    # connection drops; we await this Event instead of polling
    # ``is_closed()`` in a sleep loop (no busy-wait).
    terminated = asyncio.Event()
    try:
        conn = await _connect_asyncpg(dsn)

        def _on_terminate(_c: object) -> None:
            terminated.set()

        conn.add_termination_listener(_on_terminate)
        await conn.add_listener(channel, callback)
        logger.info("pg_listen_subscribed", extra={"channel": channel})
        # Idle until the connection drops (Event set by the termination
        # listener) or the awaiting task is cancelled.  asyncpg dispatches
        # notifications on its own reader task, so we only keep this
        # connection alive and yield control here.
        await terminated.wait()
        logger.warning(
            "pg_listen_connection_dropped", extra={"channel": channel}
        )
    except asyncio.CancelledError:
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.remove_listener(channel, callback)
            with contextlib.suppress(Exception):
                await conn.close()
        raise
    finally:
        # On a dropped connection (not cancellation, which already cleaned
        # up and re-raised) close before the caller reconnects.
        if conn is not None and not conn.is_closed():
            with contextlib.suppress(Exception):
                await conn.close()


async def pg_listen(
    channel: str,
    callback: Any,  # noqa: ANN401 - asyncpg-style cb, arbitrary by design
    *,
    reconnect_delay: float = 5.0,
) -> None:
    """Subscribe to a Postgres ``NOTIFY`` *channel* and invoke *callback*.

    Long-running loop that maintains a **dedicated** ``asyncpg`` connection
    (outside the SQLAlchemy pool — PRD §4.1) with ``conn.add_listener``.
    *callback* is the standard asyncpg listener callable
    ``cb(connection, pid, channel, payload)`` (sync **or** ``async``; asyncpg
    awaits coroutine callbacks itself).  If the connection drops, the loop
    waits ``reconnect_delay`` seconds and re-establishes ``LISTEN``.  Cancel
    the awaiting task (``task.cancel()``) to stop — the connection is closed
    cleanly on the way out.

    This never returns normally; it runs until cancelled.

    Args:
        channel:         Postgres channel name (e.g. ``"leader_trade"``).
        callback:        ``cb(conn, pid, channel, payload)`` — sync or async.
        reconnect_delay: Seconds to wait before reconnecting after a drop.

    Raises:
        asyncio.CancelledError: Propagated on task cancellation (after the
            dedicated connection has been closed).

    .. note::
        PRD §4.1: ``trader-core`` and ``discovery-worker`` communicate via
        Postgres ``LISTEN / NOTIFY`` for sub-second latency without polling.
    """
    dsn = _asyncpg_dsn()
    while True:
        try:
            await _listen_once(dsn, channel, callback)
            # Returned normally → the connection dropped; reconnect.
        except asyncio.CancelledError:
            raise  # _listen_once already closed the connection cleanly
        except Exception as exc:  # noqa: BLE001 - resilient reconnect loop
            logger.warning(
                "pg_listen_connection_lost",
                extra={"channel": channel, "error": str(exc)},
            )
        await asyncio.sleep(reconnect_delay)


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
