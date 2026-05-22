"""
tests/test_paper_models.py
==========================
Schema-contract coverage for the C1-P1 forward paper-trading-ladder data
model: the three new tables (``paper_sessions`` / ``paper_trades`` /
``paper_positions``) and the four new ``candidate_leaders`` columns
(``lifecycle_state`` / ``cooldown_until`` / ``cooldown_count`` /
``locked``).

This mirrors the in-process ``Base.metadata`` checks in
``test_models_db.py`` — tablename, primary key, foreign key, column set,
nullability, type family, and the ``lifecycle_state`` CHECK constraint.
The cross-source ORM-vs-applied-SQL exact diff is handled separately by
``test_schema_reflect.py`` (which now also imports these three models so
they register on ``Base.metadata``).

Deterministic and order-independent — no database connection is opened;
every assertion is against the SQLAlchemy ORM metadata.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from polyecho.shared.db import Base
from polyecho.shared.models import (
    CandidateLeader,
    PaperPosition,
    PaperSession,
    PaperTrade,
)


def _tbl(model: Any) -> Table:
    """The ORM model's ``Table`` — typed so ``.indexes`` / ``.constraints``
    resolve.  Mirrors the established cast in ``test_models_db.py``
    (SQLAlchemy types ``__table__`` as the broader ``FromClause``).
    """
    return cast(Table, model.__table__)


# ---------------------------------------------------------------------------
# Table registration & identity
# ---------------------------------------------------------------------------


class TestPaperTableIdentity:
    def test_tablenames(self) -> None:
        assert PaperSession.__tablename__ == "paper_sessions"
        assert PaperTrade.__tablename__ == "paper_trades"
        assert PaperPosition.__tablename__ == "paper_positions"

    def test_registered_in_metadata(self) -> None:
        tables = set(Base.metadata.tables)
        assert {"paper_sessions", "paper_trades", "paper_positions"} <= tables

    def test_primary_keys(self) -> None:
        # paper_sessions: UUID PK; paper_trades / paper_positions: BIGINT PK.
        assert [c.name for c in _tbl(PaperSession).primary_key] == ["id"]
        assert [c.name for c in _tbl(PaperTrade).primary_key] == ["id"]
        assert [c.name for c in _tbl(PaperPosition).primary_key] == ["id"]
        assert isinstance(_tbl(PaperSession).columns["id"].type, UUID)
        assert isinstance(_tbl(PaperTrade).columns["id"].type, BigInteger)
        assert isinstance(_tbl(PaperPosition).columns["id"].type, BigInteger)


# ---------------------------------------------------------------------------
# paper_sessions columns
# ---------------------------------------------------------------------------


class TestPaperSessionColumns:
    def test_column_set(self) -> None:
        cols = set(_tbl(PaperSession).columns.keys())
        assert cols == {
            "id",
            "leader_address",
            "state",
            "started_at",
            "capital_usd",
            "score",
            "trade_count",
            "copyable_pnl",
            "max_drawdown",
            "unknown_exposure_ratio",
            "filtered_ratio",
            "cursor_ts",
            "created_at",
            "updated_at",
        }

    def test_required_and_nullable(self) -> None:
        cols = _tbl(PaperSession).columns
        # NOT NULL anchors.
        for nn in (
            "leader_address",
            "state",
            "started_at",
            "capital_usd",
            "trade_count",
            "created_at",
            "updated_at",
        ):
            assert cols[nn].nullable is False, nn
        # Denormalised summary stats are NULLable — a fresh session has none.
        for nul in (
            "score",
            "copyable_pnl",
            "max_drawdown",
            "unknown_exposure_ratio",
            "filtered_ratio",
            "cursor_ts",
        ):
            assert cols[nul].nullable is True, nul

    def test_types(self) -> None:
        cols = _tbl(PaperSession).columns
        assert isinstance(cols["leader_address"].type, Text)
        assert isinstance(cols["state"].type, Text)
        assert isinstance(cols["started_at"].type, DateTime)
        assert isinstance(cols["capital_usd"].type, Numeric)
        assert isinstance(cols["score"].type, Numeric)
        assert isinstance(cols["trade_count"].type, Integer)
        assert isinstance(cols["cursor_ts"].type, DateTime)

    def test_leader_address_indexed(self) -> None:
        names = {ix.name for ix in _tbl(PaperSession).indexes}
        assert "idx_paper_sessions_leader" in names


# ---------------------------------------------------------------------------
# paper_trades columns — mirrors the backtest_trades shape
# ---------------------------------------------------------------------------


class TestPaperTradeColumns:
    def test_column_set(self) -> None:
        cols = set(_tbl(PaperTrade).columns.keys())
        assert cols == {
            "id",
            "session_id",
            "side",
            "condition_id",
            "shares",
            "price",
            "fee",
            "pnl",
            "balance_after",
            "ts",
        }

    def test_session_fk(self) -> None:
        fks = list(_tbl(PaperTrade).columns["session_id"].foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "paper_sessions"
        assert _tbl(PaperTrade).columns["session_id"].nullable is False

    def test_required_and_types(self) -> None:
        cols = _tbl(PaperTrade).columns
        assert cols["side"].nullable is False
        # condition_id / shares / price are NULLable (mirror backtest_trades).
        assert cols["condition_id"].nullable is True
        assert cols["shares"].nullable is True
        assert isinstance(cols["fee"].type, Numeric)
        assert isinstance(cols["pnl"].type, Numeric)
        assert isinstance(cols["balance_after"].type, Numeric)
        assert isinstance(cols["ts"].type, DateTime)

    def test_session_indexed(self) -> None:
        names = {ix.name for ix in _tbl(PaperTrade).indexes}
        assert "idx_paper_trades_session" in names


# ---------------------------------------------------------------------------
# paper_positions columns — open FIFO lots as a JSONB array
# ---------------------------------------------------------------------------


class TestPaperPositionColumns:
    def test_column_set(self) -> None:
        cols = set(_tbl(PaperPosition).columns.keys())
        assert cols == {"id", "session_id", "condition_id", "lots", "updated_at"}

    def test_session_fk(self) -> None:
        fks = list(_tbl(PaperPosition).columns["session_id"].foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "paper_sessions"
        assert _tbl(PaperPosition).columns["session_id"].nullable is False

    def test_lots_is_non_null_jsonb(self) -> None:
        # The open FIFO lots are a JSONB array — NOT NULL so a fresh
        # position is a valid empty list with no NULL special-case.
        col = _tbl(PaperPosition).columns["lots"]
        assert isinstance(col.type, JSONB)
        assert col.nullable is False

    def test_session_indexed(self) -> None:
        names = {ix.name for ix in _tbl(PaperPosition).indexes}
        assert "idx_paper_positions_session" in names


# ---------------------------------------------------------------------------
# candidate_leaders — the four new C1 ladder columns
# ---------------------------------------------------------------------------


class TestCandidateLeaderLifecycleColumns:
    def test_new_columns_present(self) -> None:
        cols = set(_tbl(CandidateLeader).columns.keys())
        assert {"lifecycle_state", "cooldown_until", "cooldown_count", "locked"} <= cols

    def test_user_status_untouched(self) -> None:
        # lifecycle_state is a SEPARATE column; user_status is unchanged.
        cols = _tbl(CandidateLeader).columns
        assert "user_status" in cols
        assert cols["user_status"].nullable is False

    def test_lifecycle_state_not_null_text(self) -> None:
        col = _tbl(CandidateLeader).columns["lifecycle_state"]
        assert isinstance(col.type, Text)
        assert col.nullable is False

    def test_cooldown_until_nullable(self) -> None:
        col = _tbl(CandidateLeader).columns["cooldown_until"]
        assert isinstance(col.type, DateTime)
        assert col.nullable is True

    def test_cooldown_count_not_null_integer(self) -> None:
        col = _tbl(CandidateLeader).columns["cooldown_count"]
        assert isinstance(col.type, Integer)
        assert col.nullable is False

    def test_locked_not_null_boolean(self) -> None:
        col = _tbl(CandidateLeader).columns["locked"]
        assert isinstance(col.type, Boolean)
        assert col.nullable is False

    def test_lifecycle_state_check_constraint(self) -> None:
        # The CHECK constraint must enumerate exactly the six ladder states.
        checks = [
            c
            for c in _tbl(CandidateLeader).constraints
            if c.__class__.__name__ == "CheckConstraint"
        ]
        names = {c.name for c in checks}
        assert "ck_candidate_leaders_lifecycle_state" in names
        sqltext = " ".join(str(cast(Any, c).sqltext) for c in checks)
        for state in ("discovered", "candidate", "paper", "trial_ready", "cooldown", "retired"):
            assert state in sqltext, state
