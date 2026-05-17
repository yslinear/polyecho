"""
polyecho.cli._stub
==================
Shared helper for stub commands that are not yet implemented.

Every stub prints a Rich panel indicating the command name and the PRD phase
in which it will be implemented, then exits with code 0.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

_console = Console()


def not_implemented(cmd: str, phase: int) -> None:
    """Print a Rich panel and exit cleanly.

    Args:
        cmd:   Full command path, e.g. ``"account detect"``.
        phase: PRD phase number in which this command will be implemented.
    """
    _console.print(
        Panel(
            f"[bold yellow]{cmd}[/bold yellow]: not implemented (Phase {phase})",
            title="[dim]PolyEcho CLI[/dim]",
            border_style="yellow",
        )
    )
    raise typer.Exit(code=0)
