"""
Tests for ``polyecho trader`` — Phase-4 Wave-2 trader controls.

Design
------
* importlib mode (no ``tests/__init__.py``).
* NO real network / DB: ``session_scope`` is a fake async-CM.
* ``status`` renders engine liveness + skip summary.
* ``pause-all`` / ``resume-all`` issue the right bulk status UPDATE.
* ``pause`` / ``resume`` scope the UPDATE to one leader.
* ``skips`` / ``redeems`` render and filter.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()

_LEADER = "0xLeAdEr0000000000000000000000000000000001"


@pytest.fixture(autouse=True)
def _default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default DB URL so config parse never fails on import."""
    monkeypatch.setenv(
        "POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/test"
    )


# ---------------------------------------------------------------------------
# Fake session_scope — records executed statements + serves canned results.
# ---------------------------------------------------------------------------
def _make_scope(
    *,
    scalar_one: Any = None,
    scalars_list: list[Any] | None = None,
    rows: list[Any] | None = None,
    scalars_first: Any = "__unset__",
    rowcount: int = 0,
) -> Any:
    captured: dict[str, Any] = {"executed": []}

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_execute(stmt: Any) -> MagicMock:
            captured["executed"].append(stmt)
            result = MagicMock()
            result.scalar_one_or_none.return_value = scalar_one
            result.all.return_value = rows if rows is not None else []
            scalars_obj = MagicMock()
            scalars_obj.all.return_value = (
                scalars_list if scalars_list is not None else []
            )
            if scalars_first != "__unset__":
                scalars_obj.first.return_value = scalars_first
            else:
                scalars_obj.first.return_value = (
                    scalars_list[0] if scalars_list else None
                )
            result.scalars.return_value = scalars_obj
            result.rowcount = rowcount
            return result

        session.execute = fake_execute
        session.add = MagicMock()
        captured["session"] = session
        yield session

    fake_scope._captured = captured  # type: ignore[attr-defined]
    return fake_scope


def _setting_row(key: str, value: Any) -> MagicMock:
    s = MagicMock()
    s.key = key
    s.value = value
    return s


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
class TestStatus:
    def test_status_renders_follow_counts(self) -> None:
        scope = _make_scope(
            scalar_one=None,
            rows=[("active", 3), ("paused", 1)],
        )
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["trader", "status"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "3 active" in result.output
        assert "1 paused" in result.output

    def test_status_alive_heartbeat_and_skip_summary(self) -> None:
        fresh = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        hb = _setting_row("trader_heartbeat", {"at": fresh})

        # Custom multi-result scope: heartbeat, last event, follow, skips.
        seq = [
            MagicMock(scalar_one_or_none=lambda: hb),
            MagicMock(scalar_one_or_none=lambda: None),
            MagicMock(all=lambda: [("active", 2)]),
            MagicMock(all=lambda: [("data_unavailable", 4), ("dup", 1)]),
        ]
        idx = {"i": 0}

        @asynccontextmanager
        async def scope() -> AsyncGenerator[MagicMock, None]:
            session = MagicMock()

            async def ex(_s: Any) -> MagicMock:
                r = seq[idx["i"]]
                idx["i"] += 1
                return r

            session.execute = ex
            yield session

        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["trader", "status"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "ALIVE" in result.output
        assert "data_unavailable" in result.output

    def test_status_db_error_exits_nonzero(self) -> None:
        @asynccontextmanager
        async def boom() -> AsyncGenerator[Any, None]:
            raise RuntimeError("connection refused")
            yield  # pragma: no cover

        with patch("polyecho.shared.db.session_scope", boom):
            result = runner.invoke(app, ["trader", "status"])
        assert result.exit_code != 0
        assert "Could not read trader status" in result.output


# ---------------------------------------------------------------------------
# pause-all / resume-all / pause / resume
# ---------------------------------------------------------------------------
class TestBulkPauseResume:
    def test_pause_all_sets_paused(self) -> None:
        scope = _make_scope(rowcount=7)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["trader", "pause-all", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Paused 7" in result.output
        stmt = scope._captured["executed"][0]
        compiled = str(stmt).lower()
        assert "update follow_relations" in compiled

    def test_resume_all_sets_active(self) -> None:
        scope = _make_scope(rowcount=4)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["trader", "resume-all"])
        assert result.exit_code == 0, result.output
        assert "Resumed 4" in result.output

    def test_pause_single_leader_scopes_update(self) -> None:
        scope = _make_scope(rowcount=2)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["trader", "pause", _LEADER])
        assert result.exit_code == 0, result.output
        assert "Paused 2" in result.output
        compiled = str(scope._captured["executed"][0]).lower()
        assert "update follow_relations" in compiled
        assert "where" in compiled

    def test_resume_single_leader(self) -> None:
        scope = _make_scope(rowcount=1)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["trader", "resume", _LEADER])
        assert result.exit_code == 0, result.output
        assert "Resumed 1" in result.output

    def test_pause_single_no_match_reports(self) -> None:
        scope = _make_scope(rowcount=0)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["trader", "pause", _LEADER])
        assert result.exit_code == 0, result.output
        assert "nothing changed" in result.output

    def test_pause_all_db_error_nonzero(self) -> None:
        @asynccontextmanager
        async def boom() -> AsyncGenerator[Any, None]:
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("polyecho.shared.db.session_scope", boom):
            result = runner.invoke(app, ["trader", "pause-all", "--yes"])
        assert result.exit_code != 0
        assert "pause-all failed" in result.output

    # ---- P7e: --yes / typer.confirm gate on the --all blast radius -------
    def test_pause_all_without_yes_prompts_aborts_on_no(self) -> None:
        """No ``--yes`` ⇒ a confirmation prompt; answering ``n`` aborts
        BEFORE the mutating UPDATE runs (no rows touched)."""
        scope = _make_scope(rowcount=7)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["trader", "pause-all"], input="n\n"
            )
        # typer.confirm(abort=True) raises typer.Abort ⇒ exit code 1.
        assert result.exit_code != 0
        # No mutating UPDATE was issued (count SELECT may have been).
        executed_sql = [str(s).lower() for s in scope._captured["executed"]]
        assert not any(
            "update follow_relations" in s for s in executed_sql
        ), executed_sql
        assert "About to pause" in result.output or "confirm" in result.output.lower()

    def test_pause_all_with_yes_skips_prompt(self) -> None:
        """``--yes`` ⇒ no prompt panel, no count-SELECT path."""
        scope = _make_scope(rowcount=3)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["trader", "pause-all", "--yes"]
            )
        assert result.exit_code == 0, result.output
        # The "confirm" panel must NOT have been rendered.
        assert "About to pause" not in result.output
        assert "Paused 3" in result.output

    def test_pause_all_with_yes_does_mutating_op(self) -> None:
        """``--yes`` actually issues the UPDATE follow_relations bulk write."""
        scope = _make_scope(rowcount=5)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["trader", "pause-all", "--yes"]
            )
        assert result.exit_code == 0, result.output
        executed_sql = [str(s).lower() for s in scope._captured["executed"]]
        assert any(
            "update follow_relations" in s for s in executed_sql
        ), executed_sql


# ---------------------------------------------------------------------------
# start  (handoff guidance + optional dry-run check)
# ---------------------------------------------------------------------------
class TestStart:
    def test_start_explains_handoff(self) -> None:
        result = runner.invoke(app, ["trader", "start"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        assert "docker compose up -d core" in result.output

    def test_start_check_probes_follow_counts(self) -> None:
        scope = _make_scope(rows=[("active", 5), ("paused", 0)])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["trader", "start", "--check"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "5 active" in result.output


# ---------------------------------------------------------------------------
# skips / redeems
# ---------------------------------------------------------------------------
class TestSkipsRedeems:
    def test_skips_renders_rows(self) -> None:
        r1 = MagicMock()
        r1.created_at = datetime(2026, 5, 17, tzinfo=UTC)
        r1.skip_reason = "data_unavailable"
        r1.leader_address = _LEADER
        r1.condition_id = "0xcond1"
        r1.detail = "orderbook fetch failed"
        scope = _make_scope(scalars_list=[r1])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["trader", "skips"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "data_unavailable" in result.output
        assert "orderbook fetch failed" in result.output

    def test_skips_filter_passes_reason(self) -> None:
        scope = _make_scope(scalars_list=[])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["trader", "skips", "--reason", "live_locked", "--limit", "5"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        assert "No matching skips" in result.output
        assert "live_locked" in result.output
        compiled = str(scope._captured["executed"][0]).lower()
        assert "where" in compiled

    def test_redeems_renders_and_filters(self) -> None:
        j = MagicMock()
        j.created_at = datetime(2026, 5, 17, tzinfo=UTC)
        j.status = "confirmed"
        j.condition_id = "0xcondABC"
        j.outcome_index = 1
        j.is_neg_risk = True
        j.tx_hash = "0xtxhash00000000"
        scope = _make_scope(scalars_list=[j])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["trader", "redeems", "--status", "confirmed"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        assert "confirmed" in result.output
        compiled = str(scope._captured["executed"][0]).lower()
        assert "where" in compiled

    def test_redeems_empty(self) -> None:
        scope = _make_scope(scalars_list=[])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["trader", "redeems"])
        assert result.exit_code == 0, result.output
        assert "No matching redeem jobs" in result.output
