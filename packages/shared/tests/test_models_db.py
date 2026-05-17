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
* ``pg_listen`` is an explicit ``NotImplementedError`` stub.

Deterministic & order-independent: DB engine objects are monkeypatched
to module-level ``None`` after each relevant test so no real connection
is ever opened and global state never leaks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
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
        for arr in ("sources", "risk_flags", "specialization"):
            assert isinstance(cols[arr].type, ARRAY), arr
            assert cols[arr].nullable is False
        for js in ("stats_7d", "stats_30d", "stats_90d", "frequency_stats"):
            assert isinstance(cols[js].type, JSONB), js
            assert cols[js].nullable is True

    def test_copy_template_array_and_bool_defaults(self) -> None:
        cols = CopyTemplate.__table__.columns
        assert isinstance(cols["category_whitelist"].type, ARRAY)
        assert cols["name"].unique is True
        # Booleans default True per PRD §5.6.
        assert cols["exclude_short_markets"].nullable is False
        assert cols["use_fok_orders"].nullable is False

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
        assert acc.column.table.name == "accounts"
        assert acc.ondelete == "CASCADE"
        assert tmpl.column.table.name == "copy_templates"
        assert tmpl.ondelete == "RESTRICT"

    def test_my_trade_fks(self) -> None:
        acc = self._fk(MyTrade, "account_id")
        copy = self._fk(MyTrade, "copy_of_trade_id")
        assert acc.column.table.name == "accounts"
        assert acc.ondelete == "CASCADE"
        assert copy.column.table.name == "leader_trades"
        assert copy.ondelete == "SET NULL"
        assert MyTrade.__table__.columns["copy_of_trade_id"].nullable is True


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
    monkeypatch.setattr(
        "polyecho.shared.db._get_session_factory", lambda: factory
    )
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

    def test_rollback_and_reraise_on_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_commit_failure_triggers_rollback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_get_engine_is_memoised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_session_factory_is_memoised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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


class TestPgListenStub:
    def test_pg_listen_raises_not_implemented(self) -> None:
        from polyecho.shared.db import pg_listen

        async def _cb(*_a: Any) -> None:  # pragma: no cover - never called
            return None

        with pytest.raises(NotImplementedError, match="pg_listen"):
            run(pg_listen("leader_trade", _cb))
