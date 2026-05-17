"""
Tests for packages/cli backtest + db migrate commands.

Design
------
* importlib mode (no __init__.py in tests/).
* No real DB / network — all shared-layer calls are patched.
* Uses Typer's CliRunner for black-box CLI invocation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

_FAKE_JOB_ID = uuid.uuid4()
_FAKE_JOB_ID_SHORT = str(_FAKE_JOB_ID)[:8]


def _make_fake_job(**overrides: Any) -> MagicMock:
    """Build a MagicMock that quacks like a BacktestJob ORM row."""
    job = MagicMock()
    job.id = _FAKE_JOB_ID
    job.leader_addresses = ["0xLeader1", "0xLeader2"]
    job.template_params = {
        "copy_ratio": 0.001,
        "fixed_size_usd": None,
        "max_position_usd": None,
        "min_trade_size_usd": None,
        "max_concurrent_positions": None,
        "daily_loss_limit_usd": None,
        "category_whitelist": None,
        "slippage_bps": 150,
        "execution_delay_seconds": 30,
    }
    job.start_date = date.today() - timedelta(days=90)
    job.end_date = date.today()
    job.initial_capital_usd = 1000.0
    job.slippage_bps = 150
    job.execution_delay_seconds = 30
    job.status = "pending"
    job.progress = 0
    job.result = None
    job.created_at = "2026-05-17T00:00:00+00:00"
    for k, v in overrides.items():
        setattr(job, k, v)
    return job


# ---------------------------------------------------------------------------
# Fake session_scope context manager
# ---------------------------------------------------------------------------


def _make_session_scope(add_side_effect: Any = None, scalar_result: Any = None) -> Any:
    """Return a patched session_scope that records .add() calls."""
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_flush() -> None:
            # Give the job object a real id so the caller can return it
            if session.add.call_args:
                obj = session.add.call_args[0][0]
                obj.id = _FAKE_JOB_ID

        async def fake_execute(stmt: Any) -> MagicMock:
            result = MagicMock()
            if scalar_result is not None:
                result.scalar_one_or_none.return_value = scalar_result
                result.scalars.return_value.all.return_value = (
                    scalar_result if isinstance(scalar_result, list) else [scalar_result]
                )
            else:
                result.scalar_one_or_none.return_value = None
                result.scalars.return_value.all.return_value = []
            return result

        session.flush = fake_flush
        session.execute = fake_execute
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        captured["session"] = session
        yield session

    fake_scope._captured = captured  # type: ignore[attr-defined]
    return fake_scope


# ---------------------------------------------------------------------------
# backtest new
# ---------------------------------------------------------------------------


class TestBacktestNew:
    def test_new_enqueues_row_with_correct_params(self) -> None:
        """backtest new should add one BacktestJob row with status=pending."""
        scope = _make_session_scope()

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(
                app,
                [
                    "backtest",
                    "new",
                    "--leader",
                    "0xDeadBeef",
                    "--days",
                    "90",
                    "--capital",
                    "2000",
                    "--copy-ratio",
                    "0.002",
                    "--slippage-bps",
                    "200",
                    "--delay",
                    "60",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "enqueued" in result.output.lower() or str(_FAKE_JOB_ID) in result.output

        session = scope._captured.get("session")
        assert session is not None
        session.add.assert_called_once()
        added: Any = session.add.call_args[0][0]

        assert added.leader_addresses == ["0xDeadBeef"]
        assert added.status == "pending"
        assert added.progress == 0
        assert int(added.slippage_bps) == 200
        assert int(added.execution_delay_seconds) == 60
        assert float(added.initial_capital_usd) == 2000.0
        tp = added.template_params
        assert float(tp["copy_ratio"]) == pytest.approx(0.002)
        assert int(tp["slippage_bps"]) == 200
        assert int(tp["execution_delay_seconds"]) == 60

    def test_new_multiple_leaders(self) -> None:
        """Repeating --leader should produce a list with all addresses."""
        scope = _make_session_scope()

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(
                app,
                [
                    "backtest",
                    "new",
                    "--leader",
                    "0xAAA",
                    "--leader",
                    "0xBBB",
                    "--days",
                    "30",
                ],
            )

        assert result.exit_code == 0, result.output
        session = scope._captured["session"]
        added = session.add.call_args[0][0]
        assert added.leader_addresses == ["0xAAA", "0xBBB"]

    def test_new_with_category_whitelist(self) -> None:
        """--category entries should appear in template_params."""
        scope = _make_session_scope()

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(
                app,
                [
                    "backtest",
                    "new",
                    "--leader",
                    "0xCAFE",
                    "--category",
                    "politics",
                    "--category",
                    "sports",
                ],
            )

        assert result.exit_code == 0, result.output
        session = scope._captured["session"]
        added = session.add.call_args[0][0]
        assert added.template_params["category_whitelist"] == ["politics", "sports"]

    def test_new_start_end_date_from_days(self) -> None:
        """start_date should be today - days, end_date should be today."""
        scope = _make_session_scope()

        with patch("polyecho.cli.backtest.session_scope", scope):
            runner.invoke(
                app,
                ["backtest", "new", "--leader", "0xABC", "--days", "30"],
            )

        session = scope._captured["session"]
        added = session.add.call_args[0][0]
        today = date.today()
        assert added.end_date == today
        assert added.start_date == today - timedelta(days=30)


# ---------------------------------------------------------------------------
# backtest list
# ---------------------------------------------------------------------------


class TestBacktestList:
    def test_list_renders_table(self) -> None:
        """backtest list should render a table with job rows."""
        fake_job = _make_fake_job(status="pending")
        scope = _make_session_scope(scalar_result=[fake_job])

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "list"])

        assert result.exit_code == 0, result.output
        assert "pending" in result.output

    def test_list_empty(self) -> None:
        """backtest list with no jobs should say 'No backtest jobs found'."""
        scope = _make_session_scope(scalar_result=[])

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "list"])

        assert result.exit_code == 0, result.output
        assert "No backtest jobs found" in result.output

    def test_list_completed_job(self) -> None:
        """Completed jobs should show 'completed' status in output."""
        fake_job = _make_fake_job(status="completed", progress=100)
        scope = _make_session_scope(scalar_result=[fake_job])

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "list"])

        assert result.exit_code == 0, result.output
        assert "completed" in result.output


# ---------------------------------------------------------------------------
# backtest show
# ---------------------------------------------------------------------------


class TestBacktestShow:
    def test_show_pending_job(self) -> None:
        """show on a pending job should say result not yet available."""
        fake_job = _make_fake_job(status="pending")
        scope = _make_session_scope(scalar_result=fake_job)

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "show", str(_FAKE_JOB_ID)])

        assert result.exit_code == 0, result.output
        assert "pending" in result.output
        assert "not yet available" in result.output.lower()

    def test_show_completed_with_result(self) -> None:
        """show on a completed job should render per-leader stats table."""
        result_payload = {
            "leaders": {
                "0xLeader1": {
                    "total_return_pct": 12.5,
                    "sharpe": 1.4,
                    "max_drawdown_pct": 5.0,
                    "win_rate_pct": 60.0,
                    "total_trades": 42,
                }
            },
            "scenarios": {
                "optimistic": {
                    "slippage_bps": 50,
                    "delay_seconds": 10,
                    "total_return_pct": 15.0,
                    "sharpe": 1.8,
                },
                "neutral": {
                    "slippage_bps": 150,
                    "delay_seconds": 30,
                    "total_return_pct": 12.5,
                    "sharpe": 1.4,
                },
                "pessimistic": {
                    "slippage_bps": 300,
                    "delay_seconds": 60,
                    "total_return_pct": 8.0,
                    "sharpe": 0.9,
                },
            },
        }
        fake_job = _make_fake_job(status="completed", progress=100, result=result_payload)
        scope = _make_session_scope(scalar_result=fake_job)

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "show", str(_FAKE_JOB_ID)])

        assert result.exit_code == 0, result.output
        assert "Per-Leader" in result.output
        assert "Three-Scenario" in result.output
        assert "12.5" in result.output

    def test_show_not_found(self) -> None:
        """show with a non-existent ID should exit with code 1."""
        scope = _make_session_scope(scalar_result=None)

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "show", str(uuid.uuid4())])

        assert result.exit_code == 1

    def test_show_no_result_payload(self) -> None:
        """Completed job with null result should say 'No result payload'."""
        fake_job = _make_fake_job(status="completed", progress=100, result=None)
        scope = _make_session_scope(scalar_result=fake_job)

        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "show", str(_FAKE_JOB_ID)])

        assert result.exit_code == 0, result.output
        assert "No result payload" in result.output


# ---------------------------------------------------------------------------
# db migrate
# ---------------------------------------------------------------------------


class TestDbMigrate:
    def test_migrate_reads_sql_file_and_calls_execute(self, tmp_path: Path) -> None:
        """db migrate should read 0000_init.sql and call exec_driver_sql."""
        # Create a minimal SQL file
        sql_content = "CREATE TABLE IF NOT EXISTS test_table (id INT);\n"
        sql_file = tmp_path / "0000_init.sql"
        sql_file.write_text(sql_content, encoding="utf-8")

        executed_sql: list[str] = []

        # Fake async connection context manager
        async def fake_exec_driver_sql(sql: str) -> None:
            executed_sql.append(sql)

        async def fake_commit() -> None:
            pass

        conn_mock = MagicMock()
        conn_mock.exec_driver_sql = fake_exec_driver_sql
        conn_mock.commit = fake_commit
        conn_mock.__aenter__ = AsyncMock(return_value=conn_mock)
        conn_mock.__aexit__ = AsyncMock(return_value=False)

        engine_mock = MagicMock()
        engine_mock.connect.return_value = conn_mock

        import polyecho.cli.db as db_module

        def fake_migration_path() -> Path:
            return sql_file

        with (
            patch.object(db_module, "_migration_path", fake_migration_path),
            patch("polyecho.shared.db.get_engine", return_value=engine_mock),
        ):
            result = runner.invoke(app, ["db", "migrate"])

        assert result.exit_code == 0, result.output
        assert "Applied" in result.output
        assert len(executed_sql) == 1
        assert executed_sql[0] == sql_content

    def test_migrate_missing_file_exits_nonzero(self, tmp_path: Path) -> None:
        """db migrate should exit non-zero if the SQL file doesn't exist."""
        import polyecho.cli.db as db_module

        missing = tmp_path / "nonexistent.sql"

        def fake_migration_path() -> Path:
            return missing

        with patch.object(db_module, "_migration_path", fake_migration_path):
            result = runner.invoke(app, ["db", "migrate"])

        assert result.exit_code != 0

    def test_migrate_file_not_found_error_exits_nonzero(self) -> None:
        """db migrate should exit non-zero if _migration_path raises FileNotFoundError."""
        import polyecho.cli.db as db_module

        def raise_not_found() -> Path:
            raise FileNotFoundError("no repo root")

        with patch.object(db_module, "_migration_path", raise_not_found):
            result = runner.invoke(app, ["db", "migrate"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "missing" in result.output.lower()
