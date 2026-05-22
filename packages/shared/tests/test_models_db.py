"""
tests/test_models_db.py
=======================
Schema-contract coverage for :mod:`polyecho.shared.models` and
transactional-semantics coverage for :mod:`polyecho.shared.db`.

models (PRD §6.1 — verified against the SQL DDL block)
------------------------------------------------------
* Every model maps to its exact ``__tablename__`` and the columns named
  in the PRD exist with the expected nullability / primary-key shape.
* Compound primary key on ``candidate_score_history`` (wallet+date).
* ``ARRAY(Text)`` columns (sources / risk_flags / specialization /
  category_whitelist / leader_addresses) and ``JSONB`` columns
  (stats_*, payload, result, params, value …) use the right types.
* Foreign keys + ondelete actions on ``follow_relations`` / ``my_trades``.
* ``Account`` exposes ``encrypted_private_key`` as a nullable
  ``LargeBinary`` (BYTEA) — the CLI-only secret column.
* ``Base.metadata`` actually emits ``CREATE TABLE`` (stdlib sqlite, no
  extra dependency) for the dialect-portable subset.

db
--
* ``session_scope`` commits on clean exit and rolls back + re-raises on
  error (verified with a mocked async session — no Postgres / aiosqlite).
* ``AsyncSessionLocal()`` returns a *session* (regression: it previously
  aliased the factory-getter, so the call returned a ``sessionmaker``).
* ``get_engine`` / factory are lazily memoised singletons.
* ``pg_listen`` / ``pg_listen_iter`` wire a *mocked* asyncpg connection
  (``add_listener`` / ``remove_listener`` / ``close``) — DSN normalisation,
  notification delivery, clean close, and reconnect-on-drop (no real DB).

Deterministic & order-independent: DB engine objects are monkeypatched
to module-level ``None`` after each relevant test so no real connection
is ever opened and global state never leaks.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy import BigInteger, LargeBinary
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from polyecho.shared.db import Base
from polyecho.shared.models import (
    Account,
    BacktestJob,
    CandidateLeader,
    CandidateScoreHistory,
    CopyTemplate,
    Event,
    FollowRelation,
    LeaderTrade,
    MyTrade,
    PendingAction,
    PushSubscription,
    Setting,
)

from .conftest import run

ALL_MODELS = [
    Account,
    BacktestJob,
    CandidateLeader,
    CandidateScoreHistory,
    CopyTemplate,
    Event,
    FollowRelation,
    LeaderTrade,
    MyTrade,
    PendingAction,
    PushSubscription,
    Setting,
]


# ---------------------------------------------------------------------------
# Tablenames & primary keys
# ---------------------------------------------------------------------------


class TestTableNamesAndPrimaryKeys:
    def test_tablenames_match_prd(self) -> None:
        expected = {
            Account: "accounts",
            BacktestJob: "backtest_jobs",
            CandidateLeader: "candidate_leaders",
            CandidateScoreHistory: "candidate_score_history",
            CopyTemplate: "copy_templates",
            Event: "events",
            FollowRelation: "follow_relations",
            LeaderTrade: "leader_trades",
            MyTrade: "my_trades",
            PendingAction: "pending_actions",
            PushSubscription: "push_subscriptions",
            Setting: "settings",
        }
        assert {m: m.__tablename__ for m in ALL_MODELS} == expected

    def test_every_model_registered_in_metadata(self) -> None:
        tables = set(Base.metadata.tables)
        for m in ALL_MODELS:
            assert m.__tablename__ in tables

    def test_single_column_primary_keys(self) -> None:
        single_pk = {
            Account: "id",
            CandidateLeader: "wallet_address",
            CopyTemplate: "id",
            Event: "id",
            FollowRelation: "id",
            LeaderTrade: "id",
            MyTrade: "id",
            PendingAction: "id",
            PushSubscription: "id",
            Setting: "key",
            BacktestJob: "id",
        }
        for model, col in single_pk.items():
            pk_cols = [c.name for c in model.__table__.primary_key]
            assert pk_cols == [col], f"{model.__name__} PK = {pk_cols}"

    def test_candidate_score_history_compound_pk(self) -> None:
        pk = {c.name for c in CandidateScoreHistory.__table__.primary_key}
        assert pk == {"wallet_address", "date"}


# ---------------------------------------------------------------------------
# Column existence & nullability
# ---------------------------------------------------------------------------


class TestColumns:
    def test_account_columns(self) -> None:
        cols = Account.__table__.columns
        assert set(cols.keys()) >= {
            "id",
            "name",
            "eoa_address",
            "funder_address",
            "signature_type",
            "encrypted_private_key",
            "status",
            "created_at",
            "updated_at",
        }
        assert cols["eoa_address"].unique is True
        assert cols["eoa_address"].nullable is False
        assert cols["funder_address"].nullable is True
        assert cols["name"].nullable is False

    def test_account_encrypted_key_is_nullable_largebinary(self) -> None:
        col = Account.__table__.columns["encrypted_private_key"]
        assert isinstance(col.type, LargeBinary)
        assert col.nullable is True  # NULL until CLI import

    def test_candidate_leader_array_and_jsonb_types(self) -> None:
        cols = CandidateLeader.__table__.columns
        # 0000_init.sql declares these arrays NULLable (no NOT NULL); the
        # ORM is aligned to the migration (SQL = source of truth).
        for arr in ("sources", "risk_flags", "specialization"):
            assert isinstance(cols[arr].type, ARRAY), arr
            assert cols[arr].nullable is True
        for js in ("stats_7d", "stats_30d", "stats_90d", "frequency_stats"):
            assert isinstance(cols[js].type, JSONB), js
            assert cols[js].nullable is True

    def test_copy_template_array_and_bool_defaults(self) -> None:
        cols = CopyTemplate.__table__.columns
        assert isinstance(cols["category_whitelist"].type, ARRAY)
        # SQL/Drizzle do NOT mark name UNIQUE and the booleans/arrays are
        # NULLable there (no NOT NULL) — ORM aligned to the migration.
        assert cols["name"].unique is not True
        assert cols["exclude_short_markets"].nullable is True
        assert cols["use_fok_orders"].nullable is True

    def test_copy_template_b4_keyword_filter_columns(self) -> None:
        # PolyHermes deep-dive B4: tri-mode keyword filter + upper end bound.
        cols = CopyTemplate.__table__.columns
        assert isinstance(cols["keywords"].type, ARRAY)
        assert cols["keyword_filter_mode"].nullable is True
        assert cols["keywords"].nullable is True
        assert cols["market_max_end_seconds"].nullable is True

    def test_backtest_job_arrays_and_jsonb(self) -> None:
        cols = BacktestJob.__table__.columns
        assert isinstance(cols["leader_addresses"].type, ARRAY)
        assert cols["leader_addresses"].nullable is False
        for js in ("template_params", "result"):
            assert isinstance(cols[js].type, JSONB)

    def test_setting_value_is_jsonb(self) -> None:
        assert isinstance(Setting.__table__.columns["value"].type, JSONB)

    def test_event_payload_jsonb_and_bigint_pk(self) -> None:
        cols = Event.__table__.columns
        assert isinstance(cols["payload"].type, JSONB)
        assert isinstance(cols["id"].type, BigInteger)

    def test_leader_trade_unique_trade_id(self) -> None:
        cols = LeaderTrade.__table__.columns
        assert cols["trade_id"].unique is True
        assert cols["trade_id"].nullable is False
        assert isinstance(cols["id"].type, BigInteger)
        assert isinstance(cols["raw"].type, JSONB)


# ---------------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------------


class TestForeignKeys:
    def _fk(self, model: Any, column: str) -> Any:
        (fk,) = list(model.__table__.columns[column].foreign_keys)
        return fk

    def test_follow_relation_fks(self) -> None:
        acc = self._fk(FollowRelation, "account_id")
        tmpl = self._fk(FollowRelation, "template_id")
        # 0000_init.sql uses plain ``REFERENCES`` (no ON DELETE). The ORM
        # is aligned to the migration (SQL = source of truth), so ondelete
        # is None (Postgres default NO ACTION) — not CASCADE/RESTRICT.
        assert acc.column.table.name == "accounts"
        assert acc.ondelete is None
        assert tmpl.column.table.name == "copy_templates"
        assert tmpl.ondelete is None

    def test_my_trade_account_fk_and_copy_of_trade_id(self) -> None:
        acc = self._fk(MyTrade, "account_id")
        assert acc.column.table.name == "accounts"
        assert acc.ondelete is None
        # SQL/Drizzle: ``copy_of_trade_id`` is a plain NULLable TEXT column
        # (NOT a BigInteger FK to leader_trades.id). ORM aligned to SQL.
        col = MyTrade.__table__.columns["copy_of_trade_id"]
        assert list(col.foreign_keys) == []
        assert col.nullable is True


# ---------------------------------------------------------------------------
# DDL emission — PostgreSQL dialect compilation (no DB, no extra dependency)
# ---------------------------------------------------------------------------


def _pg_create_table_ddl(model: type) -> str:
    """Return the PostgreSQL ``CREATE TABLE`` DDL for *model*'s mapped table.

    Isolated, fully-typed wrapper so the (strict-untyped) SQLAlchemy
    ``CreateTable`` / ``.compile`` calls are confined to one place. No DB
    connection is opened — only the in-process pg dialect compiler.
    """
    from typing import cast

    from sqlalchemy import Table
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    table = cast(Table, model.__table__)  # type: ignore[attr-defined]
    create: Any = CreateTable(table)
    # SQLAlchemy ships ``py.typed`` but ``postgresql.dialect`` is exposed via
    # an untyped re-export; ``Dialect`` itself is typed, so pin it explicitly
    # and keep the single unavoidable ``no-untyped-call`` scoped to one line.
    dialect: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(create.compile(dialect=dialect))


class TestMetadataCompilesToPostgresDDL:
    """``CREATE TABLE`` must compile for the *PostgreSQL* dialect.

    The models use pg-only column types (``ARRAY``, ``JSONB``) which cannot
    render on sqlite, so a sqlite ``create_all`` is the wrong oracle here.
    Compiling each ``CreateTable`` against the psycopg dialect (already
    available via SQLAlchemy — no driver connection is opened) proves the
    metadata is internally consistent *and* emits valid Postgres DDL.
    """

    def test_every_table_compiles_for_postgresql(self) -> None:
        for model in ALL_MODELS:
            ddl = _pg_create_table_ddl(model)
            assert ddl.startswith("\nCREATE TABLE")
            assert model.__tablename__ in ddl

    def test_array_and_jsonb_render_as_postgres_types(self) -> None:
        cl_ddl = _pg_create_table_ddl(CandidateLeader).upper()
        assert "TEXT[]" in cl_ddl  # ARRAY(Text)
        assert "JSONB" in cl_ddl

    def test_metadata_create_all_invokes_dialect_ddl(self) -> None:
        """``Base.metadata.create_all`` against a SQLAlchemy 2.0 *mock*
        engine must emit one ``CREATE TABLE`` per mapped table (proves the
        metadata→DDL wiring end to end, still without any DB connection)."""
        from sqlalchemy import create_mock_engine

        recorded: list[str] = []

        def _executor(sql: Any, *_a: Any, **_k: Any) -> None:
            recorded.append(str(sql.compile(dialect=engine.dialect)))

        engine = create_mock_engine("postgresql://", _executor)
        Base.metadata.create_all(engine, checkfirst=False)

        joined = "\n".join(recorded)
        for m in ALL_MODELS:
            assert f"CREATE TABLE {m.__tablename__}" in joined


# ---------------------------------------------------------------------------
# db.session_scope — commit / rollback semantics (mocked session)
# ---------------------------------------------------------------------------


def _patch_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, AsyncMock]:
    """Replace ``_get_session_factory`` with one yielding a mock session.

    Returns ``(session, factory)`` mocks so assertions can inspect
    commit/rollback calls. No engine is created.
    """
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=session)
    monkeypatch.setattr("polyecho.shared.db._get_session_factory", lambda: factory)
    return session, factory


class TestSessionScope:
    def test_commit_on_clean_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from polyecho.shared.db import session_scope

        session, _ = _patch_session_factory(monkeypatch)

        async def _use() -> None:
            async with session_scope() as s:
                assert s is session

        run(_use())
        session.commit.assert_awaited_once()
        session.rollback.assert_not_awaited()

    def test_rollback_and_reraise_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from polyecho.shared.db import session_scope

        session, _ = _patch_session_factory(monkeypatch)

        class _Boom(RuntimeError):
            pass

        async def _use() -> None:
            async with session_scope():
                raise _Boom("write failed")

        with pytest.raises(_Boom, match="write failed"):
            run(_use())

        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()

    def test_commit_failure_triggers_rollback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If ``commit()`` itself raises, the ``except`` path must still
        roll back and propagate (no swallowed error)."""
        from polyecho.shared.db import session_scope

        session, _ = _patch_session_factory(monkeypatch)
        session.commit.side_effect = RuntimeError("commit exploded")

        async def _use() -> None:
            async with session_scope():
                pass

        with pytest.raises(RuntimeError, match="commit exploded"):
            run(_use())
        session.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# db — engine/factory singletons & AsyncSessionLocal regression
# ---------------------------------------------------------------------------


class TestEngineAndFactory:
    def teardown_method(self) -> None:
        # Never leak a real engine/factory between tests.
        import polyecho.shared.db as dbmod

        dbmod._engine = None
        dbmod._session_factory = None

    def test_get_engine_is_memoised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import polyecho.shared.db as dbmod

        dbmod._engine = None
        sentinel = object()
        calls = {"n": 0}

        def _fake_create(*_a: Any, **_k: Any) -> Any:
            calls["n"] += 1
            return sentinel

        monkeypatch.setattr(dbmod, "create_async_engine", _fake_create)
        e1 = dbmod.get_engine()
        e2 = dbmod.get_engine()
        assert e1 is e2 is sentinel
        assert calls["n"] == 1  # created exactly once

    def test_session_factory_is_memoised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import polyecho.shared.db as dbmod

        dbmod._session_factory = None
        made = {"n": 0}

        # Avoid building a real engine: ``_get_session_factory`` calls
        # ``get_engine()`` to bind the maker — stub it with a sentinel.
        monkeypatch.setattr(dbmod, "get_engine", lambda: MagicMock(name="engine"))

        def _fake_maker(**_k: Any) -> Any:
            made["n"] += 1
            return MagicMock(name="factory")

        monkeypatch.setattr(dbmod, "async_sessionmaker", _fake_maker)
        f1 = dbmod._get_session_factory()
        f2 = dbmod._get_session_factory()
        assert f1 is f2
        assert made["n"] == 1

    def test_async_session_local_returns_session_not_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: ``AsyncSessionLocal`` used to be an alias of
        ``_get_session_factory`` so ``AsyncSessionLocal()`` returned a
        ``sessionmaker`` (calling it again was required to get a session,
        and ``async with AsyncSessionLocal()`` was broken). It must now
        return a fresh session object directly."""
        import polyecho.shared.db as dbmod

        session = object()
        factory = MagicMock(return_value=session)
        monkeypatch.setattr(dbmod, "_get_session_factory", lambda: factory)

        result = dbmod.AsyncSessionLocal()
        assert result is session
        factory.assert_called_once_with()


class _FakeAsyncpgConn:
    """Mocked asyncpg connection — no real DB, fully deterministic.

    Records ``add_listener`` / ``remove_listener`` / ``close`` calls and lets
    a test ``fire()`` a notification into the registered callback (the same
    contract asyncpg's reader task uses: ``cb(conn, pid, channel, payload)``,
    sync or coroutine).
    """

    def __init__(self) -> None:
        self.listeners: dict[str, Any] = {}
        self.term_listeners: list[Any] = []
        self.closed = False
        self.add_calls: list[str] = []
        self.remove_calls: list[str] = []
        self.close_calls = 0

    async def add_listener(self, channel: str, cb: Any) -> None:
        self.listeners[channel] = cb
        self.add_calls.append(channel)

    async def remove_listener(self, channel: str, _cb: Any) -> None:
        self.listeners.pop(channel, None)
        self.remove_calls.append(channel)

    def add_termination_listener(self, cb: Any) -> None:
        self.term_listeners.append(cb)

    def remove_termination_listener(self, cb: Any) -> None:
        # asyncpg has this; pg_listen_iter calls it on teardown (R2/I1).
        if cb in self.term_listeners:
            self.term_listeners.remove(cb)

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    def terminate(self) -> None:
        """Simulate the server dropping the dedicated connection."""
        self.closed = True
        for cb in self.term_listeners:
            cb(self)

    async def fire(self, channel: str, pid: int, payload: str) -> None:
        cb = self.listeners[channel]
        res = cb(self, pid, channel, payload)
        if res is not None:  # support async callbacks
            await res


class TestPgListenDsn:
    def test_strips_driver_suffix(self) -> None:
        from polyecho.shared.db import _asyncpg_dsn

        assert _asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"

    def test_plain_url_unchanged(self) -> None:
        from polyecho.shared.db import _asyncpg_dsn

        assert _asyncpg_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"

    def test_defaults_to_settings_db_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import polyecho.shared.db as dbmod

        class _S:
            # db_url is a SecretStr on Settings; _asyncpg_dsn() unwraps it
            # via .get_secret_value() — the fake must mirror that contract.
            db_url = SecretStr("postgresql+asyncpg://x:y@host:1/zz")

        monkeypatch.setattr(dbmod, "get_settings", lambda: _S())
        assert dbmod._asyncpg_dsn() == "postgresql://x:y@host:1/zz"


class TestPgListenIter:
    def test_listens_yields_and_closes_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import Notification, pg_listen_iter

        conn = _FakeAsyncpgConn()

        async def _fake_connect(_dsn: str) -> Any:
            return conn

        monkeypatch.setattr(dbmod, "_connect_asyncpg", _fake_connect)

        async def _flow() -> list[Notification]:
            got: list[Notification] = []
            async with pg_listen_iter("leader_trade", db_url="postgresql://x@h/db") as notes:
                # Listener registered on entry.
                assert conn.add_calls == ["leader_trade"]
                await conn.fire("leader_trade", 42, "hello")
                got.append(await notes.__anext__())
            return got

        out = run(_flow())
        assert len(out) == 1
        assert out[0] == Notification(pid=42, channel="leader_trade", payload="hello")
        # Cleanly torn down on context exit.
        assert conn.remove_calls == ["leader_trade"]
        assert conn.close_calls == 1

    def test_close_runs_even_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import pg_listen_iter

        conn = _FakeAsyncpgConn()
        monkeypatch.setattr(dbmod, "_connect_asyncpg", lambda _dsn: _ret(conn))

        class _Boom(RuntimeError):
            pass

        async def _flow() -> None:
            async with pg_listen_iter("c", db_url="postgresql://x@h/db"):
                raise _Boom("inner")

        with pytest.raises(_Boom, match="inner"):
            run(_flow())
        assert conn.close_calls == 1
        assert conn.remove_calls == ["c"]

    def test_registers_termination_listener(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """R2/I1: pg_listen_iter must install a termination listener so a
        dropped connection is detectable (it had none before — the
        consumer stalled forever silently)."""
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import pg_listen_iter

        conn = _FakeAsyncpgConn()
        monkeypatch.setattr(dbmod, "_connect_asyncpg", lambda _dsn: _ret(conn))

        async def _flow() -> None:
            async with pg_listen_iter("c", db_url="postgresql://x@h/db"):
                assert len(conn.term_listeners) == 1

        run(_flow())
        # Cleaned up on exit (no leaked termination listener).
        assert conn.term_listeners == []

    def test_connection_drop_raises_when_not_reconnecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R2/I1: with ``reconnect=False`` (default) a dropped connection
        must SURFACE as ``ListenConnectionLost`` instead of stalling the
        ``async for`` forever silently."""
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import ListenConnectionLost, pg_listen_iter

        conn = _FakeAsyncpgConn()
        monkeypatch.setattr(dbmod, "_connect_asyncpg", lambda _dsn: _ret(conn))

        async def _flow() -> None:
            async with pg_listen_iter("c", db_url="postgresql://x@h/db") as notes:
                conn.terminate()  # server drops the dedicated connection
                with pytest.raises(ListenConnectionLost):
                    await notes.__anext__()

        run(_flow())
        # The dropped connection is closed on the way out.
        assert conn.is_closed() is True

    def test_connection_drop_reconnects_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """R2/I1: with ``reconnect=True`` a drop transparently re-opens the
        dedicated connection and re-issues LISTEN (mirrors pg_listen) so
        the ``async for`` continues across the drop."""
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import Notification, pg_listen_iter

        conns = [_FakeAsyncpgConn(), _FakeAsyncpgConn()]
        idx = {"i": 0}

        async def _connect(_dsn: str) -> Any:
            c = conns[idx["i"]]
            idx["i"] = min(idx["i"] + 1, len(conns) - 1)
            return c

        monkeypatch.setattr(dbmod, "_connect_asyncpg", _connect)

        async def _flow() -> list[Notification]:
            got: list[Notification] = []
            async with pg_listen_iter(
                "c",
                db_url="postgresql://x@h/db",
                reconnect=True,
                reconnect_delay=0.0,
            ) as notes:
                assert conns[0].add_calls == ["c"]
                conns[0].terminate()  # drop the first connection
                # Drive the stream: it consumes the _CONN_LOST sentinel,
                # reconnects on conns[1], then yields a fresh notification.
                anext_task = asyncio.ensure_future(notes.__anext__())
                for _ in range(30):
                    await asyncio.sleep(0)
                    if conns[1].add_calls:
                        break
                assert conns[1].add_calls == ["c"]  # re-subscribed
                await conns[1].fire("c", 9, "after-reconnect")
                got.append(await anext_task)
            return got

        out = run(_flow())
        assert out == [Notification(pid=9, channel="c", payload="after-reconnect")]
        assert conns[0].is_closed() is True  # dropped conn closed
        assert conns[1].close_calls == 1  # fresh conn closed on exit


async def _ret(value: Any) -> Any:
    return value


class _Settings:
    # Mirrors Settings.db_url (SecretStr); _asyncpg_dsn unwraps via
    # .get_secret_value().
    db_url = SecretStr("postgresql+asyncpg://u@h/db")


class TestPgListenLoop:
    def test_subscribes_then_cancel_closes_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pg_listen subscribes via add_listener + termination listener,
        idles on the Event, and on task cancellation removes the listener
        and closes the dedicated connection (no real DB)."""
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import pg_listen

        conn = _FakeAsyncpgConn()
        monkeypatch.setattr(dbmod, "_connect_asyncpg", lambda _dsn: _ret(conn))
        monkeypatch.setattr(dbmod, "get_settings", lambda: _Settings())

        received: list[Any] = []

        def _cb(_c: Any, pid: int, ch: str, payload: str) -> None:
            received.append((pid, ch, payload))

        async def _flow() -> None:
            task = asyncio.ensure_future(pg_listen("leader_trade", _cb, reconnect_delay=0.01))
            # Let the loop connect + subscribe + reach `await terminated`.
            for _ in range(5):
                await asyncio.sleep(0)
                if conn.add_calls:
                    break
            assert conn.add_calls == ["leader_trade"]
            # A notification is delivered via the registered callback.
            await conn.fire("leader_trade", 7, "payload-x")
            assert received == [(7, "leader_trade", "payload-x")]
            # Cancel → clean teardown.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(_flow())
        assert conn.remove_calls == ["leader_trade"]
        assert conn.close_calls == 1

    def test_reconnects_after_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed connect is retried after ``reconnect_delay``; the second
        attempt succeeds and subscribes, then the loop is cancelled."""
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import pg_listen

        good = _FakeAsyncpgConn()
        attempts = {"n": 0}

        async def _flaky_connect(_dsn: str) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("connection refused")
            return good

        monkeypatch.setattr(dbmod, "_connect_asyncpg", _flaky_connect)
        monkeypatch.setattr(dbmod, "get_settings", lambda: _Settings())

        async def _flow() -> None:
            task = asyncio.ensure_future(pg_listen("c", lambda *_a: None, reconnect_delay=0.0))
            # Drive: fail → backoff sleep(0) → reconnect → subscribe.
            for _ in range(20):
                await asyncio.sleep(0)
                if good.add_calls:
                    break
            assert attempts["n"] == 2  # retried after the failure
            assert good.add_calls == ["c"]  # second (good) conn subscribed
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(_flow())
        assert good.close_calls == 1  # cleaned up on cancel

    def test_reconnects_after_connection_drop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the dedicated connection drops (termination listener fires),
        the loop closes it and reconnects with a fresh connection."""
        import polyecho.shared.db as dbmod
        from polyecho.shared.db import pg_listen

        conns = [_FakeAsyncpgConn(), _FakeAsyncpgConn()]
        idx = {"i": 0}

        async def _connect(_dsn: str) -> Any:
            c = conns[idx["i"]]
            idx["i"] = min(idx["i"] + 1, len(conns) - 1)
            return c

        monkeypatch.setattr(dbmod, "_connect_asyncpg", _connect)
        monkeypatch.setattr(dbmod, "get_settings", lambda: _Settings())

        async def _flow() -> None:
            task = asyncio.ensure_future(pg_listen("c", lambda *_a: None, reconnect_delay=0.0))
            for _ in range(10):
                await asyncio.sleep(0)
                if conns[0].add_calls:
                    break
            # Simulate the first connection dropping.
            conns[0].terminate()
            for _ in range(20):
                await asyncio.sleep(0)
                if conns[1].add_calls:
                    break
            assert conns[1].add_calls == ["c"]  # reconnected + re-subscribed
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(_flow())
        # The dropped connection ends up closed (the drop itself closed it);
        # the loop did not redundantly re-close an already-dead socket.
        assert conns[0].is_closed() is True
        assert conns[1].close_calls == 1  # fresh conn closed on cancel
