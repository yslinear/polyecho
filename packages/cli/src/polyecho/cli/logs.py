"""
polyecho.cli.logs
=================
Service log viewer sub-command (Phase 0).

Commands
--------
* ``polyecho logs <service>`` — tail logs for a named service
"""

from __future__ import annotations

import typer

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)

_VALID_SERVICES = [
    "trader-core",
    "discovery-worker",
    "web-push-sender",
    "dashboard-web",
    "postgres",
]


@app.command(name="show")
def show(
    service: str = typer.Argument(
        ...,
        help=f"Service name. One of: {', '.join(_VALID_SERVICES)}.",
    ),
) -> None:
    """Tail or page the logs for a PolyEcho service container."""
    not_implemented(f"logs {service}", 0)
