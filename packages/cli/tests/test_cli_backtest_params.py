"""
Hardened ``polyecho backtest`` tests focused on template_params correctness.

Complements ``test_cli_backtest.py`` with exact-shape assertions on the
``BacktestJob.template_params`` dict that ``backtest new`` constructs — this is
the contract the Phase-2 backtest engine consumes, so every key and its
default / None mapping is pinned here.

NO real DB / network: ``session_scope`` is patched with an in-memory fake.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()

_FAKE_JOB_ID = uuid.uuid4()


def _make_session_scope() -> Any:
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_flush() -> None:
            if session.add.call_args:
                session.add.call_args[0][0].id = _FAKE_JOB_ID

        session.flush = fake_flush
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        captured["session"] = session
        yield session

    fake_scope._captured = captured  # type: ignore[attr-defined]
    return fake_scope


def _run_new(*args: str) -> tuple[Any, Any]:
    scope = _make_session_scope()
    with patch("polyecho.cli.backtest.session_scope", scope):
        result = runner.invoke(app, ["backtest", "new", *args])
    assert result.exit_code == 0, result.output
    added = scope._captured["session"].add.call_args[0][0]
    return result, added


class TestTemplateParamsShape:
    def test_default_params_full_shape(self) -> None:
        """With only --leader, template_params must contain exactly the nine
        documented keys with their documented defaults / None mappings."""
        _, added = _run_new("--leader", "0xABC")
        tp = added.template_params

        assert set(tp.keys()) == {
            "copy_ratio",
            "fixed_size_usd",
            "max_position_usd",
            "min_trade_size_usd",
            "max_concurrent_positions",
            "daily_loss_limit_usd",
            "category_whitelist",
            "slippage_bps",
            "execution_delay_seconds",
        }
        assert tp["copy_ratio"] == pytest.approx(0.001)
        assert tp["fixed_size_usd"] is None
        assert tp["max_position_usd"] is None
        assert tp["min_trade_size_usd"] is None
        assert tp["max_concurrent_positions"] is None
        assert tp["daily_loss_limit_usd"] is None
        assert tp["category_whitelist"] is None
        assert tp["slippage_bps"] == 150
        assert tp["execution_delay_seconds"] == 30

        # Top-level columns mirror the defaults too.
        assert added.status == "pending"
        assert added.progress == 0
        assert float(added.initial_capital_usd) == pytest.approx(1000.0)
        assert added.slippage_bps == 150
        assert added.execution_delay_seconds == 30

    def test_optional_numeric_overrides_flow_through(self) -> None:
        """--max-position / --min-trade / --daily-loss must land in the dict."""
        _, added = _run_new(
            "--leader",
            "0xABC",
            "--max-position",
            "500",
            "--min-trade",
            "5",
            "--daily-loss",
            "50",
            "--copy-ratio",
            "0.0005",
        )
        tp = added.template_params
        assert tp["max_position_usd"] == pytest.approx(500.0)
        assert tp["min_trade_size_usd"] == pytest.approx(5.0)
        assert tp["daily_loss_limit_usd"] == pytest.approx(50.0)
        assert tp["copy_ratio"] == pytest.approx(0.0005)
        # fixed_size_usd is always None (mutually exclusive w/ ratio, not a CLI flag)
        assert tp["fixed_size_usd"] is None

    def test_empty_category_stays_none_not_empty_list(self) -> None:
        """No --category -> category_whitelist is None (not [])."""
        _, added = _run_new("--leader", "0xABC")
        assert added.template_params["category_whitelist"] is None

    def test_single_category_is_list(self) -> None:
        _, added = _run_new("--leader", "0xABC", "--category", "politics")
        assert added.template_params["category_whitelist"] == ["politics"]

    def test_date_window_matches_days(self) -> None:
        """end_date == today; start_date == today - days for a custom value."""
        _, added = _run_new("--leader", "0xABC", "--days", "7")
        today = date.today()
        assert added.end_date == today
        assert added.start_date == today - timedelta(days=7)
        # default (90d) when --days omitted
        _, added2 = _run_new("--leader", "0xABC")
        assert added2.start_date == today - timedelta(days=90)

    def test_slippage_and_delay_consistent_between_column_and_params(self) -> None:
        """slippage_bps / execution_delay_seconds must agree between the
        top-level columns and the embedded template_params dict."""
        _, added = _run_new(
            "--leader", "0xABC", "--slippage-bps", "250", "--delay", "45"
        )
        assert added.slippage_bps == 250
        assert added.execution_delay_seconds == 45
        assert added.template_params["slippage_bps"] == 250
        assert added.template_params["execution_delay_seconds"] == 45


# ---------------------------------------------------------------------------
# backtest show — exact-UUID lookup path + not-found
# ---------------------------------------------------------------------------


def _scope_returning(scalar: Any) -> Any:
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_execute(_stmt: Any) -> MagicMock:
            res = MagicMock()
            res.scalar_one_or_none.return_value = scalar
            res.scalars.return_value.all.return_value = (
                scalar if isinstance(scalar, list) else [scalar] if scalar else []
            )
            return res

        session.execute = fake_execute
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        captured["session"] = session
        yield session

    fake_scope._captured = captured  # type: ignore[attr-defined]
    return fake_scope


def _fake_job(**over: Any) -> MagicMock:
    job = MagicMock()
    job.id = _FAKE_JOB_ID
    job.leader_addresses = ["0xLeaderAAAA"]
    job.template_params = {"copy_ratio": 0.001}
    job.start_date = date.today() - timedelta(days=90)
    job.end_date = date.today()
    job.initial_capital_usd = 1000.0
    job.slippage_bps = 150
    job.execution_delay_seconds = 30
    job.status = "pending"
    job.progress = 0
    job.result = None
    job.created_at = "2026-05-17T00:00:00+00:00"
    for k, v in over.items():
        setattr(job, k, v)
    return job


class TestBacktestShowLookup:
    def test_show_valid_uuid_renders_params(self) -> None:
        """A valid UUID arg resolves via the exact-match branch and the
        template_params JSON is rendered in the panel."""
        job = _fake_job(status="pending")
        scope = _scope_returning(job)
        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "show", str(_FAKE_JOB_ID)])
        assert result.exit_code == 0, result.output
        assert "copy_ratio" in result.output

    def test_show_prefix_arg_not_found_exits_one(self) -> None:
        """A non-UUID prefix that matches nothing -> 'Job not found', exit 1."""
        scope = _scope_returning(None)
        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "show", "deadbeef"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_show_failed_job_with_result_renders(self) -> None:
        """A failed job that still has a result payload renders the tables."""
        payload = {
            "leaders": {
                "0xLeaderAAAA": {
                    "total_return_pct": -3.2,
                    "sharpe": -0.1,
                    "max_drawdown_pct": 12.0,
                    "win_rate_pct": 40.0,
                    "total_trades": 9,
                }
            }
        }
        job = _fake_job(status="failed", progress=100, result=payload)
        scope = _scope_returning(job)
        with patch("polyecho.cli.backtest.session_scope", scope):
            result = runner.invoke(app, ["backtest", "show", str(_FAKE_JOB_ID)])
        assert result.exit_code == 0, result.output
        assert "Per-Leader" in result.output
