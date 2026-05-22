"""Tests for the paper-worker money-safety boundary (C1, non-negotiable).

The paper worker is a validation layer: it must NEVER insert a
``follow_relations`` row or set ``enabled=true``.  These tests prove the
guard catches a real-money write and lets a paper-only write through.
"""

from __future__ import annotations

import pytest

from polyecho.paper.safety import (
    FORBIDDEN_TABLES,
    PaperSafetyViolation,
    assert_no_real_money_write,
    guarded_session_tables,
)


class _FakeModel:
    """A stand-in mapped object carrying a __tablename__."""

    def __init__(self, tablename: str) -> None:
        type(self).__tablename__ = tablename  # type: ignore[attr-defined]


def _obj(tablename: str) -> object:
    """A throwaway object whose TYPE advertises a __tablename__."""
    cls = type(f"M_{tablename}", (), {"__tablename__": tablename})
    return cls()


class _FakeSession:
    """Minimal SQLAlchemy-session-shaped object with new / dirty sets."""

    def __init__(self, new: list[object], dirty: list[object]) -> None:
        self.new = new
        self.dirty = dirty


class TestAssertNoRealMoneyWrite:
    def test_follow_relations_is_forbidden(self) -> None:
        assert "follow_relations" in FORBIDDEN_TABLES

    def test_paper_only_tables_pass(self) -> None:
        # No exception: the paper worker's own tables are allowed.
        assert_no_real_money_write(
            ["paper_sessions", "paper_trades", "paper_positions", "candidate_leaders"]
        )

    def test_follow_relations_write_raises(self) -> None:
        with pytest.raises(PaperSafetyViolation, match="real-money"):
            assert_no_real_money_write(["paper_trades", "follow_relations"])

    def test_violation_message_names_the_breach(self) -> None:
        with pytest.raises(PaperSafetyViolation) as exc:
            assert_no_real_money_write(["follow_relations"])
        assert "follow_relations" in str(exc.value)

    def test_empty_write_set_is_safe(self) -> None:
        assert_no_real_money_write([])


class TestGuardedSessionTables:
    def test_collects_tablenames_from_new_and_dirty(self) -> None:
        session = _FakeSession(
            new=[_obj("paper_trades"), _obj("paper_positions")],
            dirty=[_obj("paper_sessions")],
        )
        tables = guarded_session_tables(session)
        assert tables == {"paper_trades", "paper_positions", "paper_sessions"}

    def test_detects_a_forbidden_write_in_the_session(self) -> None:
        session = _FakeSession(
            new=[_obj("paper_trades"), _obj("follow_relations")],
            dirty=[],
        )
        tables = guarded_session_tables(session)
        with pytest.raises(PaperSafetyViolation):
            assert_no_real_money_write(tables)

    def test_missing_collections_do_not_crash(self) -> None:
        # A partial mock with no new/dirty must yield an empty set, not raise.
        assert guarded_session_tables(object()) == set()
