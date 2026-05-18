"""
polyecho.cli.discovery
======================
Leader candidate-pool discovery sub-commands (Phase 1).

Commands
--------
* ``polyecho discovery run [--once]``       — run discovery (one pass with
  ``--once``; the scheduled loop otherwise).
* ``polyecho discovery schedule``           — start the scheduled loop
  (alias for ``run`` without ``--once``).
* ``polyecho discovery promote <wallet>``   — flip a candidate's
  ``user_status`` to ``following`` (the §5.7 safe-write).

Design
------
The CLI is a thin operator front-end.  ``run`` / ``schedule`` reuse the
existing discovery service entry-point logic verbatim — they call
:func:`polyecho.discovery.__main__.main` (which builds the datasource and
:class:`~polyecho.discovery.worker.DiscoveryWorker` and dispatches to a
single pass or the scheduled loop).  Scoring / persistence is **never**
reimplemented here.  ``promote`` is a single safe-write UPDATE on
``candidate_leaders.user_status`` (the same surface the web UI uses).
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(no_args_is_help=True)
_console = Console()

# Allowed promotion target.  PRD §5.7 "following" is the watch-list /
# copy-eligible status the trader engine reacts to.
_FOLLOWING_STATUS = "following"


@app.command(name="run")
def run(
    once: bool = typer.Option(
        False, "--once", help="Run a single discovery pass and exit."
    ),
) -> None:
    """Run the discovery worker (all 4 sources).

    With ``--once`` this performs exactly one full daily + hourly pass and
    exits 0 (the CI / cron-friendly mode).  Without it, this hands off to
    the long-running scheduled loop (daily/hourly/weekly cadences) and only
    returns when interrupted.

    Reuses :func:`polyecho.discovery.__main__.main` verbatim so the CLI and
    the ``python -m polyecho.discovery`` service share one code path
    (scoring/persistence is never reimplemented here).
    """
    from polyecho.discovery.__main__ import main as discovery_main

    mode = "single pass (--once)" if once else "scheduled loop"
    _console.print(
        Panel(
            f"[bold]Starting discovery[/bold]: {mode}.\n"
            "[dim]All four sources via the shared DiscoveryWorker "
            "(python -m polyecho.discovery). Ctrl-C to stop the loop.[/dim]",
            title="[dim]polyecho discovery run[/dim]",
            border_style="cyan",
        )
    )

    try:
        asyncio.run(discovery_main(once=once))
    except KeyboardInterrupt:
        _console.print(
            "[yellow]Interrupted — discovery loop stopped.[/yellow]"
        )
        raise typer.Exit(code=130) from None
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Discovery failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if once:
        _console.print(
            Panel(
                "[bold green]Discovery pass complete.[/bold green]",
                title="[dim]polyecho discovery run --once[/dim]",
                border_style="green",
            )
        )


@app.command(name="schedule")
def schedule() -> None:
    """Start the discovery worker in scheduled/daemon mode.

    Equivalent to ``polyecho discovery run`` (no ``--once``): the
    daily/hourly/weekly scheduled loop via the shared service entry-point.
    Only returns when interrupted.
    """
    from polyecho.discovery.__main__ import main as discovery_main

    _console.print(
        Panel(
            "[bold]Starting discovery[/bold]: scheduled loop "
            "(daily / hourly / weekly).\n"
            "[dim]Shared DiscoveryWorker (python -m polyecho.discovery). "
            "Ctrl-C to stop.[/dim]",
            title="[dim]polyecho discovery schedule[/dim]",
            border_style="cyan",
        )
    )
    try:
        asyncio.run(discovery_main(once=False))
    except KeyboardInterrupt:
        _console.print(
            "[yellow]Interrupted — discovery loop stopped.[/yellow]"
        )
        raise typer.Exit(code=130) from None
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Discovery scheduler failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command(name="promote")
def promote(
    wallet: str = typer.Argument(
        ..., help="Wallet address (0x…) of a discovered candidate to follow."
    ),
) -> None:
    """Promote a discovered candidate to ``following`` (safe-write, §5.7).

    Sets ``candidate_leaders.user_status = 'following'`` for *wallet* — the
    same DB surface the web UI uses.  This does NOT move money: actual
    copying still requires active ``follow_relations`` AND the host
    ``POLYECHO_LIVE_TRADING`` env lock in the core engine.  Read of an
    unknown / already-rejected wallet is reported, not silently ignored.
    """
    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import CandidateLeader

    wallet_norm = wallet.strip().lower()

    async def _run() -> tuple[str, str] | None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(CandidateLeader).where(
                        CandidateLeader.wallet_address == wallet_norm
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            prev = str(row.user_status)
            row.user_status = _FOLLOWING_STATUS
            return (prev, _FOLLOWING_STATUS)

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - clean CLI error surface
        _console.print(f"[red]promote failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if result is None:
        _console.print(
            Panel(
                f"[red]No candidate found for wallet "
                f"[bold]{wallet_norm}[/bold].[/red]\n"
                "[dim]Only wallets discovered into candidate_leaders can be "
                "promoted. Run `polyecho discovery run --once` first.[/dim]",
                title="[dim]polyecho discovery promote[/dim]",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    prev, now = result
    _console.print(
        Panel(
            f"[bold green]Promoted[/bold green] candidate "
            f"[bold]{wallet_norm}[/bold]\n"
            f"user_status  {prev} → [bold green]{now}[/bold green]\n"
            "[dim]This is a safe-write only. Real copying still requires "
            "active follow_relations AND POLYECHO_LIVE_TRADING for the core "
            "engine.[/dim]",
            title="[dim]polyecho discovery promote[/dim]",
            border_style="green",
        )
    )
