"""
Tests for ``polyecho discovery`` — run / schedule / score-wallet.

Design
------
* importlib mode; Typer ``CliRunner``.
* NO real discovery / DB:
  - ``run`` / ``schedule`` reuse ``polyecho.discovery.__main__.main`` — it
    is patched (AsyncMock) so the worker never really runs; we assert the
    CLI calls it with the right ``once=`` and surfaces failures non-zero.

The legacy ``polyecho discovery promote`` sub-command (and its
``user_status='following''`` write) was removed when the
``candidate_leaders.user_status`` column was dropped.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# run / schedule  — wired to the shared discovery service entry-point
# ---------------------------------------------------------------------------
class TestDiscoveryRun:
    def test_run_once_invokes_worker_single_pass(self) -> None:
        fake_main = AsyncMock()
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(app, ["discovery", "run", "--once"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        fake_main.assert_awaited_once_with(once=True)
        assert "single pass" in result.output
        assert "complete" in result.output

    def test_run_default_invokes_scheduled_loop(self) -> None:
        fake_main = AsyncMock()
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(app, ["discovery", "run"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        fake_main.assert_awaited_once_with(once=False)
        assert "scheduled loop" in result.output

    def test_schedule_invokes_loop(self) -> None:
        fake_main = AsyncMock()
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(app, ["discovery", "schedule"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        fake_main.assert_awaited_once_with(once=False)

    def test_run_failure_exits_nonzero(self) -> None:
        fake_main = AsyncMock(side_effect=RuntimeError("datasource down"))
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(app, ["discovery", "run", "--once"], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "Discovery failed" in result.output

    def test_run_keyboard_interrupt_is_clean_nonzero(self) -> None:
        fake_main = AsyncMock(side_effect=KeyboardInterrupt())
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(app, ["discovery", "run"], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        assert "Interrupted" in result.output


# ---------------------------------------------------------------------------
# Shared async session_scope fake — reused by the score-wallet tests below.
# The legacy ``polyecho discovery promote`` sub-command (which wrote
# user_status='following') was removed when the user_status column was dropped.
# ---------------------------------------------------------------------------
def _make_scope(scalar_one: Any = "__unset__") -> Any:
    captured: dict[str, Any] = {"executed": []}

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_execute(stmt: Any) -> MagicMock:
            captured["executed"].append(stmt)
            result = MagicMock()
            result.scalar_one_or_none.return_value = (
                None if scalar_one == "__unset__" else scalar_one
            )
            return result

        session.execute = fake_execute
        captured["session"] = session
        yield session

    fake_scope._captured = captured  # type: ignore[attr-defined]
    return fake_scope


# ---------------------------------------------------------------------------
# score-wallet  — force-eval one wallet through the full per-wallet pipeline
# ---------------------------------------------------------------------------
class TestDiscoveryScoreWallet:
    """The scheduled worker is leaderboard-driven; a manually-seeded niche
    wallet never enters its Phase-2 detail set. `score-wallet` bypasses
    the budget gate for ONE wallet and writes the result back via the
    same upsert path the worker uses."""

    # score-wallet explicitly validates the EVM shape, so its tests use a
    # real 40-hex address (`0x` + 40 hex).
    _VALID = "0x" + "ab" * 20

    def _patches(self, *, is_fixture: bool, row: Any) -> tuple[Any, AsyncMock, Any]:
        # Live or fixture datasource (the command branches on isinstance).
        if is_fixture:
            from polyecho.shared.datasource.fixture import FixtureDataSource

            ds_obj: Any = FixtureDataSource()
        else:
            ds_obj = MagicMock(name="LiveDataSource-fake")

        @asynccontextmanager
        async def fake_lifecycle(ds: Any) -> AsyncGenerator[Any, None]:
            yield ds

        worker_instance = MagicMock()
        worker_instance._process_wallet = AsyncMock(return_value=None)
        worker_cls = MagicMock(return_value=worker_instance)

        scope = _make_scope(scalar_one=row)
        return (
            (ds_obj, worker_instance._process_wallet, scope),
            worker_cls,
            fake_lifecycle,
        )

    def test_score_wallet_happy_path(self) -> None:
        row = MagicMock(
            total_score=42.0,
            profit_score=60.0,
            replicability_score=70.0,
            strategy_type="Swing",
            risk_flags=["new_account"],
            sources=["external_seed"],
        )
        (ds, process_wallet, scope), worker_cls, lifecycle = self._patches(
            is_fixture=False, row=row
        )
        with (
            patch("polyecho.discovery.worker.DiscoveryWorker", worker_cls),
            patch(
                "polyecho.discovery._lifecycle.datasource_lifecycle",
                lifecycle,
            ),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=ds,
            ),
            patch("polyecho.discovery.worker.set_persistence_enabled"),
            patch("polyecho.shared.db.session_scope", scope),
        ):
            result = runner.invoke(
                app,
                [
                    "discovery",
                    "score-wallet",
                    self._VALID,
                    "--name",
                    "Theo4",
                    "--source",
                    "external_seed",
                ],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        process_wallet.assert_awaited_once()
        # Signature: _process_wallet(wallet, source, *, display_name, start=None)
        args = process_wallet.call_args.args
        kwargs = process_wallet.call_args.kwargs
        assert args[0] == self._VALID.lower()
        assert args[1] == "external_seed"
        assert kwargs.get("display_name") == "Theo4"
        assert "Scored" in result.output
        assert "42.0" in result.output

    def test_score_wallet_invalid_address(self) -> None:
        result = runner.invoke(
            app,
            ["discovery", "score-wallet", "not-an-address"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        assert "Invalid EVM address" in result.output

    def test_score_wallet_refuses_fixture_mode(self) -> None:
        (ds, _process, _scope), worker_cls, lifecycle = self._patches(is_fixture=True, row=None)
        with (
            patch(
                "polyecho.discovery._lifecycle.datasource_lifecycle",
                lifecycle,
            ),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=ds,
            ),
        ):
            result = runner.invoke(
                app,
                ["discovery", "score-wallet", self._VALID],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 2
        assert "fixture" in result.output.lower()

    def test_score_wallet_row_exists_but_unscored_exits_nonzero(self) -> None:
        """A thin-row exists from a prior upsert but _process_wallet's
        fetch failed (e.g. data-api / proxy transient). total_score is
        still NULL — must NOT mis-print 'Scored', must exit non-zero."""
        thin = MagicMock(total_score=None)
        (ds, _process, scope), worker_cls, lifecycle = self._patches(is_fixture=False, row=thin)
        with (
            patch("polyecho.discovery.worker.DiscoveryWorker", worker_cls),
            patch(
                "polyecho.discovery._lifecycle.datasource_lifecycle",
                lifecycle,
            ),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=ds,
            ),
            patch("polyecho.discovery.worker.set_persistence_enabled"),
            patch("polyecho.shared.db.session_scope", scope),
        ):
            result = runner.invoke(
                app,
                ["discovery", "score-wallet", self._VALID],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code != 0
        assert "Row exists but NOT scored" in result.output
        assert "Scored " not in result.output  # space matters

    def test_score_wallet_no_row_persisted_exits_nonzero(self) -> None:
        (ds, _process, scope), worker_cls, lifecycle = self._patches(is_fixture=False, row=None)
        with (
            patch("polyecho.discovery.worker.DiscoveryWorker", worker_cls),
            patch(
                "polyecho.discovery._lifecycle.datasource_lifecycle",
                lifecycle,
            ),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=ds,
            ),
            patch("polyecho.discovery.worker.set_persistence_enabled"),
            patch("polyecho.shared.db.session_scope", scope),
        ):
            result = runner.invoke(
                app,
                ["discovery", "score-wallet", self._VALID],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code != 0
        assert "No row persisted" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["discovery", "--help"],
        ["discovery", "run", "--help"],
        ["discovery", "schedule", "--help"],
        ["discovery", "score-wallet", "--help"],
    ],
    ids=lambda a: " ".join(a),
)
def test_discovery_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
