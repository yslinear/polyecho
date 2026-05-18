"""
polyecho.cli._stub
==================
Shared helper for stub commands that are not yet implemented.

Every stub prints a Rich panel naming the command + the PRD phase it will
land in, then exits **non-zero** (code 2).

Why non-zero (C-1/M4 hardening)
-------------------------------
``typer.Exit(code=0)`` is *silent success*: a CI step or operator script
that runs an unimplemented command would see exit 0 and assume the action
happened.  An unimplemented command MUST fail loudly so a pipeline that
depends on it breaks visibly instead of pretending to succeed.  Code ``2``
mirrors the conventional "command line / usage" failure class (and is what
Click/Typer use for usage errors), distinguishing it from a real runtime
error (1).
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

_console = Console()

# Exit code for an unimplemented command.  Non-zero by design so scripts /
# CI never mistake "not implemented" for success.
NOT_IMPLEMENTED_EXIT_CODE = 2


def not_implemented(cmd: str, phase: int) -> None:
    """Print a Rich panel and exit **non-zero** (the command did nothing).

    Args:
        cmd:   Full command path, e.g. ``"account detect"``.
        phase: PRD phase number in which this command will be implemented.

    Raises:
        typer.Exit: always, with code :data:`NOT_IMPLEMENTED_EXIT_CODE`
            (2) so callers/CI see a clear failure rather than a silent
            success.
    """
    _console.print(
        Panel(
            f"[bold yellow]{cmd}[/bold yellow]: not implemented (Phase {phase})\n"
            "[dim]This command is a stub — it performed no action and is "
            "exiting non-zero so scripts/CI do not treat it as success.[/dim]",
            title="[dim]PolyEcho CLI — NOT IMPLEMENTED[/dim]",
            border_style="yellow",
        )
    )
    raise typer.Exit(code=NOT_IMPLEMENTED_EXIT_CODE)
