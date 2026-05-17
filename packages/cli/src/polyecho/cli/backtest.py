"""
polyecho.cli.backtest
=====================
Backtest engine sub-commands (Phase 2).

Commands
--------
* ``polyecho backtest new --leader <addr> --days <n>`` — create a new backtest job
* ``polyecho backtest list``                            — list historical backtest results
* ``polyecho backtest show <id>``                       — display a backtest result in detail
"""

from __future__ import annotations

import typer

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)

_PHASE = 2


@app.command(name="new")
def new(
    leader: str = typer.Option(..., "--leader", help="Leader wallet address (0x…)."),
    days: int = typer.Option(90, "--days", help="Number of historical days to backtest."),
) -> None:
    """Create and queue a new backtest job for a leader address."""
    not_implemented(f"backtest new --leader {leader} --days {days}", _PHASE)


@app.command(name="list")
def list_backtests() -> None:
    """List all historical backtest jobs and their status."""
    not_implemented("backtest list", _PHASE)


@app.command(name="show")
def show(
    id: str = typer.Argument(..., help="Backtest job UUID."),  # noqa: A002
) -> None:
    """Display the full result of a completed backtest job."""
    not_implemented(f"backtest show {id}", _PHASE)
