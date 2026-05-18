"""
Tests for ``polyecho db reconcile`` — wired to the FROZEN
``polyecho.core.onchain_reconcile`` contract (Wave C-cli).

Frozen contract (provided by the parallel C-reconcile agent)
------------------------------------------------------------
* ``polyecho.core.onchain_reconcile.run_reconcile`` — async, returns a
  ``ReconcileReport``.
* ``python -m polyecho.core.onchain_reconcile --once`` — single pass,
  exit 0 on success/no-drift, non-zero on a real error.

The frozen module is delivered out-of-band, so these tests inject a fake
``polyecho.core.onchain_reconcile`` into ``sys.modules`` (import path) and
mock ``subprocess.run`` (fallback path).  Neither real RPC nor real DB is
touched.  Contract asserted: ``run_reconcile`` is awaited; the report
summary is printed; drift is REPORTED not failed (exit 0) unless
``--strict``; a real reconcile error exits non-zero.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()


class _FakeReport:
    """ReconcileReport-shaped: ``summary()`` + ``drift_count``."""

    def __init__(self, *, drift_count: int, text: str) -> None:
        self.drift_count = drift_count
        self._text = text

    def summary(self) -> str:
        return self._text


def _install_fake_module(run_reconcile: object) -> types.ModuleType:
    mod = types.ModuleType("polyecho.core.onchain_reconcile")
    mod.run_reconcile = run_reconcile  # type: ignore[attr-defined]
    return mod


class TestDbReconcileImportPath:
    def test_calls_run_reconcile_and_prints_report(self) -> None:
        report = _FakeReport(drift_count=0, text="no drift: 12 positions OK")
        fake_run = AsyncMock(return_value=report)
        fake_mod = _install_fake_module(fake_run)
        with patch.dict(
            sys.modules,
            {"polyecho.core.onchain_reconcile": fake_mod},
        ):
            result = runner.invoke(
                app, ["db", "reconcile"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        fake_run.assert_awaited_once()
        assert "no drift: 12 positions OK" in result.output

    def test_drift_reported_not_failed_by_default(self) -> None:
        """Drift detected ⇒ exit 0 (reported, not a failure)."""
        report = _FakeReport(drift_count=3, text="DRIFT: 3 positions mismatch")
        fake_mod = _install_fake_module(AsyncMock(return_value=report))
        with patch.dict(
            sys.modules, {"polyecho.core.onchain_reconcile": fake_mod}
        ):
            result = runner.invoke(
                app, ["db", "reconcile"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "DRIFT: 3 positions mismatch" in result.output

    def test_strict_makes_drift_nonzero(self) -> None:
        report = _FakeReport(drift_count=2, text="DRIFT: 2 mismatch")
        fake_mod = _install_fake_module(AsyncMock(return_value=report))
        with patch.dict(
            sys.modules, {"polyecho.core.onchain_reconcile": fake_mod}
        ):
            result = runner.invoke(
                app, ["db", "reconcile", "--strict"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "drift(s) detected" in result.output

    def test_strict_no_drift_is_zero(self) -> None:
        report = _FakeReport(drift_count=0, text="clean")
        fake_mod = _install_fake_module(AsyncMock(return_value=report))
        with patch.dict(
            sys.modules, {"polyecho.core.onchain_reconcile": fake_mod}
        ):
            result = runner.invoke(
                app, ["db", "reconcile", "--strict"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output

    def test_reconcile_error_exits_nonzero(self) -> None:
        fake_run = AsyncMock(side_effect=RuntimeError("RPC unreachable"))
        fake_mod = _install_fake_module(fake_run)
        with patch.dict(
            sys.modules, {"polyecho.core.onchain_reconcile": fake_mod}
        ):
            result = runner.invoke(
                app, ["db", "reconcile"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "Reconcile failed" in result.output


class TestDbReconcileSubprocessFallback:
    """When ``run_reconcile`` cannot be imported, fall back to
    ``python -m polyecho.core.onchain_reconcile --once``."""

    def _force_import_error(self) -> object:
        # A module object WITHOUT run_reconcile → `from … import
        # run_reconcile` raises ImportError, triggering the fallback.
        return types.ModuleType("polyecho.core.onchain_reconcile")

    def test_fallback_invokes_python_m_once(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        bare_mod = self._force_import_error()
        with (
            patch.dict(
                sys.modules,
                {"polyecho.core.onchain_reconcile": bare_mod},
            ),
            patch(
                "polyecho.cli.db.subprocess.run", return_value=proc
            ) as run_mock,
        ):
            result = runner.invoke(
                app, ["db", "reconcile"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        assert argv[1:] == [
            "-m",
            "polyecho.core.onchain_reconcile",
            "--once",
        ]
        assert "Reconcile complete" in result.output

    def test_fallback_strict_forwards_flag(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        bare_mod = self._force_import_error()
        with (
            patch.dict(
                sys.modules,
                {"polyecho.core.onchain_reconcile": bare_mod},
            ),
            patch(
                "polyecho.cli.db.subprocess.run", return_value=proc
            ) as run_mock,
        ):
            result = runner.invoke(
                app, ["db", "reconcile", "--strict"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "--strict" in run_mock.call_args[0][0]

    def test_fallback_nonzero_exit_propagates(self) -> None:
        proc = MagicMock()
        proc.returncode = 1
        bare_mod = self._force_import_error()
        with (
            patch.dict(
                sys.modules,
                {"polyecho.core.onchain_reconcile": bare_mod},
            ),
            patch("polyecho.cli.db.subprocess.run", return_value=proc),
        ):
            result = runner.invoke(
                app, ["db", "reconcile"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "Reconcile error" in result.output


@pytest.mark.parametrize(
    "args",
    [["db", "--help"], ["db", "reconcile", "--help"]],
    ids=lambda a: " ".join(a),
)
def test_db_reconcile_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
