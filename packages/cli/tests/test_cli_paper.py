"""
Tests for ``polyecho paper`` - status / run / promote (C1 paper-ladder CLI).

Design
------
* importlib mode; Typer ``CliRunner``.
* NO real paper worker / DB:
  - ``status`` drives a fake ``session_scope`` async-CM that returns canned
    per-state counts + open-session rows.
  - ``run`` patches ``PaperWorker`` so ``run_cycle`` returns a canned
    ``CycleResult``; we assert the counters render and the disabled-flag
    notice appears.
  - ``promote`` drives a fake ``session_scope`` and patches the danger-
    passphrase helpers; we assert it mirrors the web ``promoteToLiveFollow``
    gate order (rejects non-trial_ready, danger-gates, inserts a PAUSED row).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()

_WALLET = "0x" + "ab" * 20


# ---------------------------------------------------------------------------
# Fake session_scope - returns a queue of scalar_one_or_none / .all() results.
# ---------------------------------------------------------------------------
def _make_scope(*, scalars: list[Any] | None = None, alls: list[list[Any]] | None = None) -> Any:
    """Build a fake ``session_scope`` async-CM.

    ``scalars`` is consumed (in order) by ``.scalar_one_or_none()`` calls;
    ``alls`` is consumed (in order) by ``.all()`` calls. A flushed row keeps
    its in-memory attributes so ``promote`` can read back account/template.
    """
    state: dict[str, Any] = {
        "scalars": list(scalars or []),
        "alls": list(alls or []),
        "added": [],
    }

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_execute(_stmt: Any) -> MagicMock:
            result = MagicMock()
            if state["scalars"]:
                result.scalar_one_or_none.return_value = state["scalars"].pop(0)
            else:
                result.scalar_one_or_none.return_value = None
            if state["alls"]:
                result.all.return_value = state["alls"].pop(0)
            else:
                result.all.return_value = []
            return result

        async def fake_flush() -> None:
            return None

        session.execute = fake_execute
        session.flush = fake_flush
        session.add = lambda obj: state["added"].append(obj)
        state["session"] = session
        yield session

    fake_scope._state = state  # type: ignore[attr-defined]
    return fake_scope


# ===========================================================================
# status
# ===========================================================================
class TestPaperStatus:
    def test_status_renders_ladder_and_sessions(self) -> None:
        counts = [("paper", 3), ("trial_ready", 1), ("discovered", 5)]
        sessions = [
            (_WALLET, "paper", 12, 4.25, -0.08, 71.5, None),
        ]
        scope = _make_scope(alls=[counts, sessions])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["paper", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        assert "Paper-trading ladder" in result.output
        assert "Open paper sessions" in result.output
        assert "trial_ready" in result.output
        assert _WALLET in result.output

    def test_status_no_open_sessions(self) -> None:
        scope = _make_scope(alls=[[("discovered", 2)], []])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["paper", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        assert "No open paper sessions" in result.output

    def test_status_db_error_exits_nonzero(self) -> None:
        @asynccontextmanager
        async def boom() -> AsyncGenerator[Any, None]:
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("polyecho.shared.db.session_scope", boom):
            result = runner.invoke(app, ["paper", "status"], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "paper status failed" in result.output


# ===========================================================================
# run
# ===========================================================================
def _cycle_result(*, skipped: bool = False, sessions: int = 0, trades: int = 0) -> Any:
    """A real CycleResult (no transitions) for the run-command tests."""
    from polyecho.paper.worker import CycleResult

    return CycleResult(
        skipped_locked_cycle=skipped,
        sessions_simulated=sessions,
        trades_simulated=trades,
        transitions=[],
    )


class TestPaperRun:
    def test_run_triggers_one_cycle(self) -> None:
        worker = MagicMock()

        async def fake_run_cycle() -> Any:
            return _cycle_result(sessions=2, trades=9)

        worker.run_cycle = fake_run_cycle
        worker_cls = MagicMock(return_value=worker)
        settings = MagicMock(paper_trading_enabled=True)
        with (
            patch("polyecho.paper.worker.PaperWorker", worker_cls),
            patch("polyecho.shared.config.get_settings", return_value=settings),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=MagicMock(name="ds"),
            ),
        ):
            result = runner.invoke(app, ["paper", "run"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        assert "Paper cycle complete" in result.output
        assert "sessions_simulated  2" in result.output
        assert "trades_simulated    9" in result.output

    def test_run_prints_notice_when_flag_off(self) -> None:
        worker = MagicMock()

        async def fake_run_cycle() -> Any:
            return _cycle_result(sessions=0, trades=0)

        worker.run_cycle = fake_run_cycle
        worker_cls = MagicMock(return_value=worker)
        settings = MagicMock(paper_trading_enabled=False)
        with (
            patch("polyecho.paper.worker.PaperWorker", worker_cls),
            patch("polyecho.shared.config.get_settings", return_value=settings),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=MagicMock(name="ds"),
            ),
        ):
            result = runner.invoke(app, ["paper", "run"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        # The flag is off → the disabled-loop notice must be shown, but the
        # manual cycle still runs.
        assert "automated paper-trading loop is disabled" in result.output
        assert "Paper cycle complete" in result.output

    def test_run_skipped_locked_cycle(self) -> None:
        worker = MagicMock()

        async def fake_run_cycle() -> Any:
            return _cycle_result(skipped=True)

        worker.run_cycle = fake_run_cycle
        worker_cls = MagicMock(return_value=worker)
        settings = MagicMock(paper_trading_enabled=True)
        with (
            patch("polyecho.paper.worker.PaperWorker", worker_cls),
            patch("polyecho.shared.config.get_settings", return_value=settings),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=MagicMock(name="ds"),
            ),
        ):
            result = runner.invoke(app, ["paper", "run"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        assert "Cycle skipped" in result.output

    def test_run_failure_exits_nonzero(self) -> None:
        worker = MagicMock()

        async def fake_run_cycle() -> Any:
            raise RuntimeError("datasource down")

        worker.run_cycle = fake_run_cycle
        worker_cls = MagicMock(return_value=worker)
        settings = MagicMock(paper_trading_enabled=True)
        with (
            patch("polyecho.paper.worker.PaperWorker", worker_cls),
            patch("polyecho.shared.config.get_settings", return_value=settings),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=MagicMock(name="ds"),
            ),
        ):
            result = runner.invoke(app, ["paper", "run"], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "paper run failed" in result.output


# ===========================================================================
# promote - mirrors the web promoteToLiveFollow gate order
# ===========================================================================
class TestPaperPromote:
    _STORED = "pbkdf2$100000$" + "A" * 24 + "$" + "B" * 44

    def test_promote_rejects_non_trial_ready(self) -> None:
        """Gate (d): a candidate not in `trial_ready` is rejected before any
        write and before the passphrase is even prompted."""
        candidate = MagicMock(locked=False, lifecycle_state="paper")
        scope = _make_scope(scalars=[candidate])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["paper", "promote", _WALLET], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "trial_ready" in result.output
        assert "`paper`" in result.output

    def test_promote_rejects_locked(self) -> None:
        candidate = MagicMock(locked=True, lifecycle_state="trial_ready")
        scope = _make_scope(scalars=[candidate])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["paper", "promote", _WALLET], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "locked" in result.output

    def test_promote_rejects_unknown_candidate(self) -> None:
        scope = _make_scope(scalars=[None])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["paper", "promote", _WALLET], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "No candidate leader found" in result.output

    def test_promote_danger_gate_rejects_bad_passphrase(self) -> None:
        """Gate (a): all structural gates pass, but a wrong passphrase fails
        the constant-time verify → non-zero, nothing written."""
        candidate = MagicMock(locked=False, lifecycle_state="trial_ready")
        # preflight scalars: candidate, follow(none), account_id, template_id
        scope = _make_scope(scalars=[candidate, None, "acct-1", "tmpl-1"])
        with (
            patch("polyecho.shared.db.session_scope", scope),
            patch(
                "polyecho.shared.danger_passphrase.get_danger_passphrase_hash",
                MagicMock(),
            ),
            patch(
                "polyecho.shared.danger_passphrase.verify_danger_passphrase",
                return_value=False,
            ),
        ):
            # get_danger_passphrase_hash is async - patch with an async stub.
            with patch(
                "polyecho.shared.danger_passphrase.get_danger_passphrase_hash",
                new=_async_return(self._STORED),
            ):
                result = runner.invoke(
                    app,
                    ["paper", "promote", _WALLET],
                    input="wrong-secret\n",
                    env={"COLUMNS": "200"},
                )
        assert result.exit_code != 0
        assert "verification failed" in result.output
        # No follow row was added.
        assert scope._state["added"] == []

    def test_promote_danger_gate_unprovisioned(self) -> None:
        """Gate (a): danger passphrase not provisioned at all → rejected."""
        candidate = MagicMock(locked=False, lifecycle_state="trial_ready")
        scope = _make_scope(scalars=[candidate, None, "acct-1", "tmpl-1"])
        with (
            patch("polyecho.shared.db.session_scope", scope),
            patch(
                "polyecho.shared.danger_passphrase.get_danger_passphrase_hash",
                new=_async_return(None),
            ),
        ):
            result = runner.invoke(app, ["paper", "promote", _WALLET], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "not provisioned" in result.output

    def test_promote_inserts_paused_follow_row(self) -> None:
        """Happy path: all gates pass, a correct passphrase verifies, and
        EXACTLY ONE follow_relations row is inserted at status='paused'."""
        candidate = MagicMock(locked=False, lifecycle_state="trial_ready")
        # preflight: candidate, follow(none), account, template
        # write: candidate, follow(none), account, template
        scope = _make_scope(
            scalars=[
                candidate,
                None,
                "acct-1",
                "tmpl-1",
                candidate,
                None,
                "acct-1",
                "tmpl-1",
            ]
        )
        with (
            patch("polyecho.shared.db.session_scope", scope),
            patch(
                "polyecho.shared.danger_passphrase.get_danger_passphrase_hash",
                new=_async_return(self._STORED),
            ),
            patch(
                "polyecho.shared.danger_passphrase.verify_danger_passphrase",
                return_value=True,
            ),
            patch(
                "polyecho.shared.danger_passphrase.is_danger_passphrase_provisioned",
                new=_async_return(True),
            ),
        ):
            result = runner.invoke(
                app,
                ["paper", "promote", _WALLET],
                input="correct-secret\n",
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        assert "Promoted" in result.output
        # Exactly one follow_relations row, status='paused'.
        added = scope._state["added"]
        assert len(added) == 1
        from polyecho.shared.models import FollowRelation

        row = added[0]
        assert isinstance(row, FollowRelation)
        assert row.status == "paused"
        assert row.leader_address == _WALLET.lower()
        assert row.account_id == "acct-1"
        assert row.template_id == "tmpl-1"

    def test_promote_rejects_no_account(self) -> None:
        candidate = MagicMock(locked=False, lifecycle_state="trial_ready")
        # candidate, follow(none), account(none)
        scope = _make_scope(scalars=[candidate, None, None])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["paper", "promote", _WALLET], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "No account is registered" in result.output


def _async_return(value: Any) -> Any:
    """Build an AsyncMock-like coroutine function returning *value*."""

    async def _coro(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _coro


# ===========================================================================
# help - every group + subcommand renders complete help
# ===========================================================================
@pytest.mark.parametrize(
    "args",
    [
        ["paper", "--help"],
        ["paper", "status", "--help"],
        ["paper", "run", "--help"],
        ["paper", "promote", "--help"],
    ],
    ids=lambda a: " ".join(a),
)
def test_paper_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output


def test_promote_help_states_danger_and_paused() -> None:
    """The promote --help must plainly state it is danger-gated and creates
    a PAUSED follow (an explicit task requirement)."""
    result = runner.invoke(app, ["paper", "promote", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    out = result.output.upper()
    assert "DANGER" in out
    assert "PAUSED" in out
