"""
polyecho.cli.paper
==================
Paper-trading ladder sub-commands (C1).

C1 shipped a forward paper-trading subsystem (``packages/paper``): a six-state
ladder on ``candidate_leaders.lifecycle_state``
(``discovered -> candidate -> paper -> trial_ready``, with ``cooldown`` /
``retired`` side states), the ``paper_sessions`` / ``paper_trades`` /
``paper_positions`` tables, and a ~15-min worker
(:meth:`polyecho.paper.worker.PaperWorker.run_cycle`).  It has a ``/research``
web page but no CLI; the other PolyEcho features are web + CLI dual-track, so
this module closes the gap.

Commands
--------
* ``polyecho paper status``        - read-only ladder view (per-state counts +
  the open ``paper_sessions`` table).
* ``polyecho paper run``           - manually trigger ONE ``run_cycle()`` and
  print the resulting :class:`~polyecho.paper.worker.CycleResult`.
* ``polyecho paper promote <eoa>`` - the CLI equivalent of the web
  ``promoteToLiveFollow``: a danger-gated, fail-closed promotion of a
  ``trial_ready`` candidate into ONE ``follow_relations`` row at
  ``status='paused'`` (the operator must still Resume it).

Design
------
The CLI is a thin operator front-end.  ``run`` reuses
:class:`~polyecho.paper.worker.PaperWorker` verbatim - simulation / scoring /
the lifecycle state machine are NEVER reimplemented here.  ``promote`` mirrors
the EXACT gate order of the web ``promoteToLiveFollow`` Server Action
(``packages/web/app/research/actions.ts``), fail-closed: it is the only CLI
code that INSERTs a ``follow_relations`` row, and it does so PAUSED.
"""

from __future__ import annotations

import asyncio
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
_console = Console()

#: The single ladder state a candidate must be in to be promotable to a
#: real follow.  Mirrors gate (d) of the web ``promoteToLiveFollow``.
_TRIAL_READY = "trial_ready"

#: The six ladder states, in forward order, for a stable status table.
_LADDER_STATES = (
    "discovered",
    "candidate",
    "paper",
    "trial_ready",
    "cooldown",
    "retired",
)


def _fmt_num(value: Any, digits: int = 2) -> str:
    """Format an optional Numeric column for table display ('-' when None)."""
    if value is None:
        return "-"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


def _fmt_ts(value: Any) -> str:
    """Format an optional timestamp for table display ('-' when None)."""
    if value is None:
        return "-"
    try:
        return str(value.strftime("%Y-%m-%d %H:%M"))
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        return str(value)


# ---------------------------------------------------------------------------
# status  - read-only ladder view
# ---------------------------------------------------------------------------
@app.command(name="status")
def status() -> None:
    """Show the paper-trading ladder (read-only - never writes the DB).

    Renders two things:

    * the count of ``candidate_leaders`` per ``lifecycle_state`` (the six
      ladder states ``discovered`` / ``candidate`` / ``paper`` /
      ``trial_ready`` / ``cooldown`` / ``retired``); and
    * a table of the open ``paper_sessions`` - one row per leader currently
      being paper-traded, with its ladder state, simulated ``trade_count``,
      ``copyable_pnl``, ``max_drawdown``, ``score`` and ``cursor_ts``.

    This is a pure read: it mirrors the ``/research`` web page and writes
    nothing.
    """
    from sqlalchemy import func, select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import CandidateLeader, PaperSession

    async def _load() -> tuple[dict[str, int], list[tuple[Any, ...]]]:
        async with session_scope() as session:
            count_rows = (
                await session.execute(
                    select(
                        CandidateLeader.lifecycle_state,
                        func.count(),
                    ).group_by(CandidateLeader.lifecycle_state)
                )
            ).all()
            counts = {str(state): int(n) for state, n in count_rows}

            # Open sessions, joined to their candidate for the ladder state.
            session_rows = (
                await session.execute(
                    select(
                        PaperSession.leader_address,
                        CandidateLeader.lifecycle_state,
                        PaperSession.trade_count,
                        PaperSession.copyable_pnl,
                        PaperSession.max_drawdown,
                        PaperSession.score,
                        PaperSession.cursor_ts,
                    )
                    .join(
                        CandidateLeader,
                        CandidateLeader.wallet_address == PaperSession.leader_address,
                        isouter=True,
                    )
                    .where(PaperSession.state == "open")
                    .order_by(PaperSession.started_at)
                )
            ).all()
            return counts, [tuple(r) for r in session_rows]

    try:
        counts, sessions = asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]paper status failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # -- ladder per-state counts -------------------------------------------
    ladder = Table(title="Paper-trading ladder", title_style="bold")
    ladder.add_column("Lifecycle state")
    ladder.add_column("Candidates", justify="right")
    total = 0
    for state in _LADDER_STATES:
        n = counts.get(state, 0)
        total += n
        ladder.add_row(state, str(n))
    # Surface any state outside the known six (defensive - should not occur
    # given the candidate_leaders CHECK constraint).
    for state, n in sorted(counts.items()):
        if state not in _LADDER_STATES:
            total += n
            ladder.add_row(f"[yellow]{state}[/yellow]", str(n))
    ladder.add_row("[dim]total[/dim]", f"[dim]{total}[/dim]")
    _console.print(ladder)

    # -- open paper sessions -----------------------------------------------
    if not sessions:
        _console.print(
            Panel(
                "No open paper sessions.\n"
                "[dim]A session opens when the ladder promotes a candidate "
                "into the `paper` state. Run `polyecho paper run` to advance "
                "the ladder one cycle.[/dim]",
                title="[dim]polyecho paper status[/dim]",
                border_style="yellow",
            )
        )
        return

    table = Table(title="Open paper sessions", title_style="bold")
    table.add_column("Leader")
    table.add_column("State")
    table.add_column("Trades", justify="right")
    table.add_column("Copyable PnL", justify="right")
    table.add_column("Max drawdown", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Cursor ts")

    for leader, state, trade_count, copyable_pnl, max_dd, score, cursor_ts in sessions:
        state_style = {
            "paper": "cyan",
            "trial_ready": "bold green",
            "cooldown": "yellow",
            "retired": "dim",
        }.get(str(state), "white")
        table.add_row(
            str(leader),
            f"[{state_style}]{state or '-'}[/{state_style}]",
            str(trade_count if trade_count is not None else 0),
            _fmt_num(copyable_pnl, 4),
            _fmt_num(max_dd, 4),
            _fmt_num(score, 2),
            _fmt_ts(cursor_ts),
        )
    _console.print(table)


# ---------------------------------------------------------------------------
# run  - manually trigger ONE PaperWorker cycle
# ---------------------------------------------------------------------------
@app.command(name="run")
def run() -> None:
    """Manually run ONE paper-trading cycle and print the result.

    Triggers a single :meth:`polyecho.paper.worker.PaperWorker.run_cycle`
    in-process: every open ``paper`` session is simulated forward, the
    ``paper_sessions`` summaries are recomputed, and the lifecycle state
    machine advances every eligible candidate.  The :class:`CycleResult`
    counters (``sessions_simulated`` / ``trades_simulated`` / the ladder
    ``transitions``) are then printed.

    The automated paper-worker loop (``python -m polyecho.paper``) is gated
    by the ``paper_trading_enabled`` Setting and does nothing when it is
    off.  A manual ``paper run`` is the operator EXPLICITLY asking for one
    cycle, so it runs regardless of that flag - but it prints a clear
    notice when the flag is off so the operator knows the automated loop is
    still disabled.

    This NEVER trades real money: the paper worker is a validation layer
    and its commit-time guard refuses any ``follow_relations`` write.
    """
    # Honour the same datasource lifecycle the paper service uses: a
    # LiveDataSource must be entered as an async CM before any data call;
    # a FixtureDataSource has no I/O lifecycle.
    from contextlib import AbstractAsyncContextManager

    from polyecho.paper.worker import PaperWorker
    from polyecho.shared.config import get_settings
    from polyecho.shared.datasource import get_datasource

    try:
        enabled = bool(get_settings().paper_trading_enabled)
    except Exception:  # noqa: BLE001 - settings read must not crash the notice
        enabled = False

    if not enabled:
        _console.print(
            Panel(
                "[yellow]The automated paper-trading loop is disabled[/yellow] "
                "(paper_trading_enabled = false).\n"
                "[dim]Running ONE manual cycle now because you explicitly "
                "asked. The scheduled `python -m polyecho.paper` service stays "
                "inert until the Setting is turned on.[/dim]",
                title="[dim]polyecho paper run[/dim]",
                border_style="yellow",
            )
        )

    async def _run() -> Any:
        ds = get_datasource()
        if isinstance(ds, AbstractAsyncContextManager):
            async with ds:
                return await PaperWorker(ds).run_cycle()
        return await PaperWorker(ds).run_cycle()

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]paper run failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if result.skipped_locked_cycle:
        _console.print(
            Panel(
                "[yellow]Cycle skipped[/yellow] - another paper worker holds "
                "the advisory overlap lock.\n"
                "[dim]Nothing was simulated. Retry once the other cycle "
                "finishes.[/dim]",
                title="[dim]polyecho paper run[/dim]",
                border_style="yellow",
            )
        )
        return

    changed = [t for t in result.transitions if t.changed]
    transition_lines = (
        "\n".join(
            f"  {t.from_state.value} → [bold]{t.to_state.value}[/bold] ({t.reason})"
            for t in changed
        )
        if changed
        else "  [dim]none[/dim]"
    )
    _console.print(
        Panel(
            f"[bold green]Paper cycle complete.[/bold green]\n"
            f"sessions_simulated  {result.sessions_simulated}\n"
            f"trades_simulated    {result.trades_simulated}\n"
            f"transitions         {len(changed)} changed "
            f"of {len(result.transitions)} evaluated\n"
            f"{transition_lines}",
            title="[dim]polyecho paper run[/dim]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# promote  - the CLI equivalent of the web promote-to-live (danger-gated)
# ---------------------------------------------------------------------------
@app.command(name="promote")
def promote(
    eoa: str = typer.Argument(
        ...,
        help="Wallet address (0x…) of a `trial_ready` candidate leader to "
        "promote to a real (but PAUSED) follow.",
    ),
) -> None:
    """Promote a `trial_ready` candidate to a real but PAUSED follow.

    ⚠ DANGER-GATED, money-path. This is the CLI equivalent of the web
    ``promoteToLiveFollow`` Server Action - the single explicit operator
    action that bridges the C1 paper ladder to real money. The paper
    worker NEVER does this on its own; ``trial_ready`` is advisory only.

    Gate order (FAIL CLOSED - nothing is written until ALL pass), mirroring
    the web action EXACTLY:

      (b) the candidate exists;
      (c) it is NOT ``locked`` (an operator override skips it);
      (d) its ``lifecycle_state`` is exactly ``trial_ready`` - any other
          ladder state is rejected;
      (e) it has no existing ``follow_relations`` row (idempotent);
      (f) a default account AND a default copy_template both exist to link;
      (a) the operator danger passphrase is verified (constant-time hash
          compare against the CLI-provisioned secret - the same gate
          ``polyecho account set-danger-passphrase`` provisions).

    Only then is ONE ``follow_relations`` row INSERTed at
    ``status='paused'``. Even a successful promotion does NOT start
    copy-trading: the operator must still explicitly Resume the follow on
    the web ``/leaders`` page (itself danger-confirmed). Double gate.

    The danger passphrase is read via a hidden ``typer.prompt`` only - it
    is NEVER a CLI argument and never lands in argv / shell history / logs.
    """
    from sqlalchemy import asc, select

    from polyecho.shared.danger_passphrase import (
        get_danger_passphrase_hash,
        is_danger_passphrase_provisioned,
        verify_danger_passphrase,
    )
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import (
        Account,
        CandidateLeader,
        CopyTemplate,
        FollowRelation,
    )

    leader_address = eoa.strip().lower()

    # -- pre-flight READ: run gates (b)-(f) before prompting for the secret,
    # so an operator is never asked for the passphrase on a request that
    # would be rejected anyway. The authoritative re-check happens inside
    # the write transaction below (a Server Action is a public RPC; here
    # the equivalent risk is a TOCTOU race, so we re-verify on write).
    async def _preflight() -> tuple[str, str | None]:
        async with session_scope() as session:
            candidate = (
                await session.execute(
                    select(CandidateLeader).where(CandidateLeader.wallet_address == leader_address)
                )
            ).scalar_one_or_none()
            if candidate is None:
                return ("no_candidate", None)
            if bool(candidate.locked):
                return ("locked", None)
            if str(candidate.lifecycle_state) != _TRIAL_READY:
                return (f"bad_state:{candidate.lifecycle_state}", None)
            existing = (
                await session.execute(
                    select(FollowRelation.id).where(FollowRelation.leader_address == leader_address)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return ("already_followed", None)
            account_id = (
                await session.execute(select(Account.id).order_by(asc(Account.created_at)).limit(1))
            ).scalar_one_or_none()
            if account_id is None:
                return ("no_account", None)
            template_id = (
                await session.execute(
                    select(CopyTemplate.id).order_by(asc(CopyTemplate.name)).limit(1)
                )
            ).scalar_one_or_none()
            if template_id is None:
                return ("no_template", None)
            # All structural gates pass. Also surface whether the danger
            # passphrase is provisioned at all (CLI-only provisioning).
            stored = await get_danger_passphrase_hash(session)
            return ("ok", stored)

    def _reject(message: str) -> NoReturn:
        _console.print(
            Panel(
                f"[red]{message}[/red]",
                title="[dim]polyecho paper promote[/dim]",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    try:
        outcome, stored_hash = asyncio.run(_preflight())
    except Exception as exc:  # noqa: BLE001 - clean CLI error surface
        _console.print(f"[red]paper promote failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if outcome == "no_candidate":
        _reject(
            f"No candidate leader found for {leader_address}.\n"
            "Only a discovered + paper-traded candidate can be promoted."
        )
    if outcome == "locked":
        _reject("This candidate leader is locked (operator override) and cannot be promoted.")
    if outcome.startswith("bad_state:"):
        actual = outcome.split(":", 1)[1]
        _reject(
            f"Only a `trial_ready` candidate can be promoted - this one is "
            f"`{actual}`. The paper ladder must advance it to `trial_ready` "
            "first."
        )
    if outcome == "already_followed":
        _reject("This leader already has a follow relation - not creating a duplicate.")
    if outcome == "no_account":
        _reject("No account is registered - import one with `polyecho account import <eoa>` first.")
    if outcome == "no_template":
        _reject("No copy template exists - create one before promoting.")

    # -- danger-passphrase gate (a). Fail closed if the operator has not
    # provisioned a passphrase at all (it is CLI-provisioned only).
    if stored_hash is None:
        _reject(
            "The operator danger passphrase is not provisioned. Run "
            "`polyecho account set-danger-passphrase` first."
        )

    _console.print(
        Panel(
            f"Leader           [bold]{leader_address}[/bold]\n"
            f"Lifecycle state  [bold green]{_TRIAL_READY}[/bold green]\n"
            f"Action           INSERT one follow_relations row "
            "[bold]status=paused[/bold]\n"
            "[yellow]⚠ This is a money-path action. The follow is created "
            "PAUSED - you must still Resume it on the web /leaders page "
            "(itself danger-confirmed) before any copy-trading starts.[/yellow]",
            title="[dim]polyecho paper promote (danger-gated)[/dim]",
            border_style="yellow",
        )
    )

    # Hidden passphrase prompt - NOT a Typer Option, so it can never appear
    # in argv / shell history / logs (matches account.py).
    passphrase = typer.prompt("Operator danger passphrase (hidden)", hide_input=True)
    if not passphrase or not passphrase.strip():
        _console.print("[red]Empty passphrase - aborted.[/red]")
        raise typer.Exit(code=1)

    if not verify_danger_passphrase(passphrase, stored_hash):
        # Best-effort reference drop (CPython str is immutable; not zeroed).
        passphrase = ""  # noqa: F841
        _reject("Danger passphrase verification failed - nothing written.")
    passphrase = ""  # noqa: F841

    # -- WRITE transaction: re-check every structural gate (TOCTOU guard)
    # then INSERT exactly one paused follow_relations row.
    async def _write() -> tuple[str, str] | str:
        async with session_scope() as session:
            candidate = (
                await session.execute(
                    select(CandidateLeader).where(CandidateLeader.wallet_address == leader_address)
                )
            ).scalar_one_or_none()
            if candidate is None:
                return "no_candidate"
            if bool(candidate.locked):
                return "locked"
            if str(candidate.lifecycle_state) != _TRIAL_READY:
                return f"bad_state:{candidate.lifecycle_state}"
            existing = (
                await session.execute(
                    select(FollowRelation.id).where(FollowRelation.leader_address == leader_address)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return "already_followed"
            account_id = (
                await session.execute(select(Account.id).order_by(asc(Account.created_at)).limit(1))
            ).scalar_one_or_none()
            if account_id is None:
                return "no_account"
            template_id = (
                await session.execute(
                    select(CopyTemplate.id).order_by(asc(CopyTemplate.name)).limit(1)
                )
            ).scalar_one_or_none()
            if template_id is None:
                return "no_template"
            # Re-verify the passphrase is still provisioned at write time.
            if not await is_danger_passphrase_provisioned(session):
                return "no_passphrase"

            follow = FollowRelation(
                account_id=account_id,
                leader_address=leader_address,
                template_id=template_id,
                status="paused",
            )
            session.add(follow)
            await session.flush()
            return (str(account_id), str(template_id))

    try:
        write_result = asyncio.run(_write())
    except Exception as exc:  # noqa: BLE001 - clean CLI error surface
        _console.print(f"[red]paper promote failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if isinstance(write_result, str):
        # A gate that passed pre-flight but failed at write time (a race).
        _reject(
            f"Promotion aborted at write time ({write_result}) - the "
            "candidate state changed concurrently. Nothing was written."
        )

    account_id, template_id = write_result
    _console.print(
        Panel(
            f"[bold green]Promoted[/bold green] candidate "
            f"[bold]{leader_address}[/bold] to a real follow.\n"
            f"account_id   {account_id}\n"
            f"template_id  {template_id}\n"
            f"status       [bold yellow]paused[/bold yellow]\n"
            "[yellow]⚠ Copy-trading has NOT started. Resume the follow on "
            "the web /leaders page to begin.[/yellow]\n"
            "[dim]The default account / template were linked; re-point them "
            "on /leaders before resuming if needed.[/dim]",
            title="[dim]polyecho paper promote[/dim]",
            border_style="green",
        )
    )
