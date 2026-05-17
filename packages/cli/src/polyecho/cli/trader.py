"""
polyecho.cli.trader
===================
Trader-core process management sub-commands (Phase 4).

Commands
--------
* ``polyecho trader start``              — start the trader-core process
* ``polyecho trader status``             — show current trader status
* ``polyecho trader pause-all``          — globally pause all copy-trading
* ``polyecho trader pause <leader>``     — pause copy-trading for a specific leader
"""

from __future__ import annotations

import typer

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)

_PHASE = 4


@app.command(name="start")
def start() -> None:
    """Start the trader-core process (holds private key, no open ports)."""
    not_implemented("trader start", _PHASE)


@app.command(name="status")
def status() -> None:
    """Show the current status of trader-core and all active follow relations."""
    not_implemented("trader status", _PHASE)


@app.command(name="pause-all")
def pause_all() -> None:
    """Globally pause all copy-trading (sets global_pause flag in DB settings)."""
    not_implemented("trader pause-all", _PHASE)


@app.command(name="pause")
def pause(
    leader_addr: str = typer.Argument(..., help="Leader wallet address (0x…) to pause."),
) -> None:
    """Pause copy-trading for a specific leader address."""
    not_implemented(f"trader pause {leader_addr}", _PHASE)
