"""
polyecho.cli.discovery
======================
Leader candidate-pool discovery sub-commands (Phase 1).

Commands
--------
* ``polyecho discovery run [--once]``       — run discovery (once or loop)
* ``polyecho discovery schedule``           — start scheduled discovery mode
* ``polyecho discovery promote <wallet>``   — manually add a wallet to the watch list
"""

from __future__ import annotations

import typer

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)

_PHASE = 1


@app.command(name="run")
def run(
    once: bool = typer.Option(False, "--once", help="Run a single discovery pass and exit."),
) -> None:
    """Run the discovery worker (all 4 sources) immediately."""
    not_implemented("discovery run" + (" --once" if once else ""), _PHASE)


@app.command(name="schedule")
def schedule() -> None:
    """Start the discovery worker in scheduled/daemon mode."""
    not_implemented("discovery schedule", _PHASE)


@app.command(name="promote")
def promote(
    wallet: str = typer.Argument(..., help="Wallet address (0x…) to add to the watch list."),
) -> None:
    """Manually promote a wallet address into the candidate-pool watch list."""
    not_implemented(f"discovery promote {wallet}", _PHASE)
