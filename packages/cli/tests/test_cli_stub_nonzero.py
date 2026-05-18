"""
Regression: the shared stub helper exits **non-zero** (C-1/M4).

A stub that exits 0 is *silent success* — a CI step / operator script
that runs an unimplemented command would think the action happened.  After
Wave C-cli every previously-stubbed command (``discovery run/--once/
schedule/promote``, ``logs show``, ``db reconcile``) is implemented, so no
production command is wired to ``not_implemented`` anymore.  This test
locks the **contract** so any *future* stub added via the shared helper
fails loudly (non-zero) instead of pretending to succeed, and it proves no
src command still resolves to the helper.

NO real DB / network.
"""

from __future__ import annotations

import pathlib

import pytest
import typer
from typer.testing import CliRunner

from polyecho.cli.__main__ import app
from polyecho.cli._stub import NOT_IMPLEMENTED_EXIT_CODE, not_implemented

runner = CliRunner()

_CLI_SRC = pathlib.Path(
    "packages/cli/src/polyecho/cli"
)


def test_exit_code_constant_is_nonzero() -> None:
    assert NOT_IMPLEMENTED_EXIT_CODE != 0
    assert NOT_IMPLEMENTED_EXIT_CODE == 2


def test_not_implemented_helper_raises_nonzero_exit() -> None:
    """The shared helper itself: any future stub exits non-zero, never 0."""
    with pytest.raises(typer.Exit) as exc:
        not_implemented("some future cmd", 9)
    assert exc.value.exit_code == NOT_IMPLEMENTED_EXIT_CODE
    assert exc.value.exit_code != 0


def test_no_src_command_is_still_a_silent_stub() -> None:
    """After Wave C-cli, NO command module (other than the helper module
    that defines it) should call ``not_implemented`` — every stub now has a
    real implementation."""
    if not _CLI_SRC.is_dir():
        pytest.skip("run from repo root; cli src path not resolvable")
    offenders: list[str] = []
    for py in _CLI_SRC.glob("*.py"):
        if py.name == "_stub.py":
            continue
        text = py.read_text(encoding="utf-8")
        if "not_implemented(" in text:
            offenders.append(py.name)
    assert offenders == [], (
        f"these command modules still call not_implemented (silent-stub "
        f"risk): {offenders}"
    )


def test_help_does_not_regress_for_implemented_groups() -> None:
    """Sanity: the formerly-stubbed groups now expose real --help (exit 0),
    not a NOT IMPLEMENTED panel."""
    for args in (
        ["discovery", "--help"],
        ["logs", "--help"],
        ["db", "--help"],
    ):
        result = runner.invoke(app, args, env={"COLUMNS": "200"})
        assert result.exit_code == 0, (args, result.output)
        assert "NOT IMPLEMENTED" not in result.output
