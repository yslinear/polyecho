"""
Tests for ``polyecho discovery`` — run / schedule / promote (Wave C-cli).

Design
------
* importlib mode; Typer ``CliRunner``.
* NO real discovery / DB:
  - ``run`` / ``schedule`` reuse ``polyecho.discovery.__main__.main`` — it
    is patched (AsyncMock) so the worker never really runs; we assert the
    CLI calls it with the right ``once=`` and surfaces failures non-zero.
  - ``promote`` flips ``candidate_leaders.user_status`` via a fake
    ``session_scope`` async-CM (mirrors the trader/account test fakes).
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

_WALLET = "0xCaNdIdAtE000000000000000000000000000001"


# ---------------------------------------------------------------------------
# run / schedule  — wired to the shared discovery service entry-point
# ---------------------------------------------------------------------------
class TestDiscoveryRun:
    def test_run_once_invokes_worker_single_pass(self) -> None:
        fake_main = AsyncMock()
        with patch(
            "polyecho.discovery.__main__.main", fake_main
        ):
            result = runner.invoke(
                app, ["discovery", "run", "--once"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        fake_main.assert_awaited_once_with(once=True)
        assert "single pass" in result.output
        assert "complete" in result.output

    def test_run_default_invokes_scheduled_loop(self) -> None:
        fake_main = AsyncMock()
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(
                app, ["discovery", "run"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        fake_main.assert_awaited_once_with(once=False)
        assert "scheduled loop" in result.output

    def test_schedule_invokes_loop(self) -> None:
        fake_main = AsyncMock()
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(
                app, ["discovery", "schedule"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        fake_main.assert_awaited_once_with(once=False)

    def test_run_failure_exits_nonzero(self) -> None:
        fake_main = AsyncMock(side_effect=RuntimeError("datasource down"))
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(
                app, ["discovery", "run", "--once"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "Discovery failed" in result.output

    def test_run_keyboard_interrupt_is_clean_nonzero(self) -> None:
        fake_main = AsyncMock(side_effect=KeyboardInterrupt())
        with patch("polyecho.discovery.__main__.main", fake_main):
            result = runner.invoke(
                app, ["discovery", "run"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "Interrupted" in result.output


# ---------------------------------------------------------------------------
# promote  — safe-write: user_status → following
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


class TestDiscoveryPromote:
    def test_promote_sets_following(self) -> None:
        row = MagicMock()
        row.user_status = "new"
        scope = _make_scope(scalar_one=row)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["discovery", "promote", _WALLET], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert row.user_status == "following"
        assert "Promoted" in result.output
        assert "following" in result.output

    def test_promote_unknown_wallet_exits_nonzero(self) -> None:
        scope = _make_scope(scalar_one=None)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["discovery", "promote", _WALLET], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "No candidate found" in result.output

    def test_promote_db_error_exits_nonzero(self) -> None:
        @asynccontextmanager
        async def boom() -> AsyncGenerator[Any, None]:
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("polyecho.shared.db.session_scope", boom):
            result = runner.invoke(
                app, ["discovery", "promote", _WALLET], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "promote failed" in result.output

    def test_promote_normalises_wallet_lowercase(self) -> None:
        row = MagicMock()
        row.user_status = "watching"
        scope = _make_scope(scalar_one=row)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["discovery", "promote", _WALLET.upper()],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        assert _WALLET.lower() in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["discovery", "--help"],
        ["discovery", "run", "--help"],
        ["discovery", "schedule", "--help"],
        ["discovery", "promote", "--help"],
    ],
    ids=lambda a: " ".join(a),
)
def test_discovery_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
