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
    copying still requires active ``follow_relations``.  Read of an
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
            "active follow_relations.[/dim]",
            title="[dim]polyecho discovery promote[/dim]",
            border_style="green",
        )
    )


@app.command(name="score-wallet")
def score_wallet(
    wallet: str = typer.Argument(
        ..., help="EVM address (0x + 40 hex) to force-evaluate."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Optional display_name to upsert if not set."
    ),
    source: str = typer.Option(
        "external_seed",
        "--source",
        help="Provenance tag merged into candidate_leaders.sources.",
    ),
) -> None:
    """Force-evaluate ONE wallet via the full discovery pipeline.

    The scheduled worker is leaderboard-driven — Phase-1 only touches
    the top-200 PnL leaderboard and Phase-2 detail-scores a budgeted
    subset of THAT set.  A wallet inserted manually (e.g. a niche
    smart-money seed not in the current top-200) therefore never
    receives the full evaluation.  This command bypasses the
    budget gate for one wallet and runs the SAME per-wallet pipeline the
    worker uses internally: fetch activity + positions → frequency
    stats → strategy + risk flags → profit / replicability / total
    scores → UPSERT ``candidate_leaders``.

    Refuses to run under ``POLYECHO_DATASOURCE=fixture`` — discovery
    persistence is unconditionally closed there (no synthetic data ever
    reaches the operator DB; no opt-in to bypass).
    """
    import re

    from sqlalchemy import select

    from polyecho.discovery._lifecycle import datasource_lifecycle
    from polyecho.discovery.worker import (
        DiscoveryWorker,
        set_persistence_enabled,
    )
    from polyecho.shared.datasource import (
        FixtureDataSource,
        get_datasource,
    )
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import CandidateLeader

    _EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
    w = wallet.strip()
    if not _EVM_RE.match(w):
        _console.print(
            f"[red]Invalid EVM address[/red]: {w}\n"
            "[dim]Expected 0x followed by exactly 40 hex chars.[/dim]"
        )
        raise typer.Exit(code=2)
    w_norm = w.lower()

    async def _evaluate_and_read() -> CandidateLeader | None:
        async with datasource_lifecycle(get_datasource()) as ds:
            if isinstance(ds, FixtureDataSource):
                _console.print(
                    "[red]Refusing in fixture datasource mode[/red] — "
                    "discovery persistence is unconditionally closed for "
                    "fixture (no synthetic data ever reaches the operator "
                    "DB). Set POLYECHO_DATASOURCE=live and retry."
                )
                raise typer.Exit(code=2)
            # Live path: ensure the worker's persistence gate is open
            # (defensive — the gate defaults open; this guards against a
            # prior test/process toggling it shut in the same interpreter).
            set_persistence_enabled(True)
            worker = DiscoveryWorker(ds)
            await worker._process_wallet(
                w_norm, source, display_name=name
            )

        async with session_scope() as session:
            return (
                await session.execute(
                    select(CandidateLeader).where(
                        CandidateLeader.wallet_address == w_norm
                    )
                )
            ).scalar_one_or_none()

    try:
        row = asyncio.run(_evaluate_and_read())
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - clean CLI error surface
        _console.print(f"[red]score-wallet failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if row is None:
        _console.print(
            Panel(
                f"[yellow]No row persisted for {w_norm}[/yellow]\n"
                "[dim]The per-wallet fetch likely failed (data-api / proxy "
                "issue, or the address has no Polymarket activity). Check "
                "`polyecho doctor` and retry.[/dim]",
                title="[dim]polyecho discovery score-wallet[/dim]",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=1)

    # The row may already exist from a prior thin upsert (e.g. an external-
    # seed UPSERT writing display_name + sources before any scoring). In
    # that case ``_process_wallet`` runs but the per-wallet activity /
    # positions fetch can fail (transient data-api / proxy issue, circuit
    # breaker tripping during the run, or the wallet simply has no recent
    # Polymarket trades) — the scoring fields stay NULL. Without this guard
    # the CLI would mis-report "Scored" on a row that was never actually
    # scored this run. Surface the gap honestly + non-zero exit so the
    # operator knows to retry.
    if row.total_score is None:
        _console.print(
            Panel(
                f"[yellow]Row exists but NOT scored[/yellow] for {w_norm}\n"
                "[dim]The per-wallet activity / positions fetch failed "
                "during this run (transient data-api / proxy issue, or "
                "the upstream circuit breaker tripped). The row is in "
                "the pool as a thin candidate; retry later — a fresh cli "
                "run, or the next scheduled discovery cycle, will attempt "
                "the fetch again.[/dim]",
                title="[dim]polyecho discovery score-wallet[/dim]",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=1)

    _console.print(
        Panel(
            f"[bold green]Scored[/bold green] {w_norm}\n"
            f"  total_score         = {row.total_score}\n"
            f"  profit_score        = {row.profit_score}\n"
            f"  replicability_score = {row.replicability_score}\n"
            f"  strategy_type       = {row.strategy_type}\n"
            f"  risk_flags          = {list(row.risk_flags or [])}\n"
            f"  sources             = {list(row.sources or [])}",
            title="[dim]polyecho discovery score-wallet[/dim]",
            border_style="green",
        )
    )
