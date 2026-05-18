"""
polyecho.cli.trader
===================
Trader-core *control* sub-commands (Phase-4 Wave-2).

Control model (PRD §4.2 / §5.7)
-------------------------------
The CLI **never holds private keys and never places orders** — the engine
that signs and trades is the long-running ``core`` service
(``python -m polyecho.core`` / ``docker compose``).  Operators steer it
purely by mutating Postgres rows the engine reacts to:

* ``follow_relations.status`` ∈ ``{active, paused}`` — per-leader on/off.
* ``settings`` rows — audit / acknowledgement trail.

Operation tiers (PRD §5.7)
--------------------------
* **safe-write** — ``pause-all`` / ``resume-all`` / ``pause`` / ``resume``
  flip ``follow_relations.status`` (the same surface the web UI uses).
* **dangerous, CLI-only** — ``enable-live`` runs the §5.7 dangerous-tier
  ritual: risk explanation → 5-second countdown → hidden passphrase
  verified by *decrypt-check* against a stored account (never logged) →
  writes the ``live_trading_ack`` audit row.  It does **NOT** move money:
  the binding live-money switch is the host env var
  ``POLYECHO_LIVE_TRADING`` consumed by
  :func:`polyecho.shared.copytrading.assert_live_allowed`.  There is
  intentionally **no web path** to this.

Commands
--------
* ``polyecho trader status``                — liveness + follow counts +
  LIVE-TRADING LOCKED/UNLOCKED + recent skip summary.
* ``polyecho trader pause-all``             — pause every follow_relation.
* ``polyecho trader resume-all``            — resume every follow_relation.
* ``polyecho trader pause <leader>``        — pause one leader.
* ``polyecho trader resume <leader>``       — resume one leader.
* ``polyecho trader start``                 — handoff guidance + optional
  one-shot dry-run check (never spawns the daemon).
* ``polyecho trader enable-live``           — DANGEROUS ack ritual.
* ``polyecho trader disable-live``          — clear the ack row.
* ``polyecho trader skips [--limit][--reason]`` — query ``copy_skips``.
* ``polyecho trader redeems [--status]``    — list ``redeem_jobs``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
_console = Console()

# Settings keys this module owns (audit / heartbeat surface).
_HEARTBEAT_KEY = "trader_heartbeat"
_LIVE_ACK_KEY = "live_trading_ack"

# Heartbeat older than this ⇒ the engine is considered DOWN/STALE.
_HEARTBEAT_STALE_SECONDS = 90

# Countdown length for the dangerous enable-live ritual (PRD §5.7).
# Indirected through a module-level function so tests can patch it to a
# no-op and stay deterministic without sleeping for real.
_COUNTDOWN_SECONDS = 5


def _countdown(seconds: int = _COUNTDOWN_SECONDS) -> None:
    """Visible N-second abort window for the dangerous tier (PRD §5.7).

    Patched to a no-op in tests (``patch("polyecho.cli.trader._countdown")``)
    so the suite is deterministic and never sleeps for real.
    """
    for remaining in range(seconds, 0, -1):
        _console.print(
            f"[bold red] {remaining} [/bold red]… "
            f"[dim](Ctrl-C to abort)[/dim]"
        )
        time.sleep(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _short(addr: str | None) -> str:
    """Abbreviate an address for table display (``0x1234…abcd``)."""
    if not addr:
        return "-"
    return addr if len(addr) <= 14 else f"{addr[:8]}…{addr[-6:]}"


def _fmt_ts(value: Any) -> str:
    """Best-effort human timestamp (accepts datetime or ISO string)."""
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    return str(value)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
@app.command(name="status")
def status() -> None:
    """Show trader-core liveness, follow counts, the live lock, and skips.

    Read-only.  Liveness is inferred from a ``settings`` heartbeat row
    (``trader_heartbeat`` ``{at: <iso>}``) the engine is expected to
    refresh; falls back to the most recent ``events`` row.  The
    LIVE-TRADING banner reflects
    :func:`polyecho.shared.copytrading.is_live_enabled` (the real host
    env lock) — never this CLI's audit row.
    """
    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    from polyecho.shared.copytrading import is_live_enabled
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import CopySkip, Event, FollowRelation, Setting

    async def _run() -> dict[str, Any]:
        async with session_scope() as session:
            hb_row = (
                await session.execute(
                    select(Setting).where(Setting.key == _HEARTBEAT_KEY)
                )
            ).scalar_one_or_none()

            last_event = (
                await session.execute(
                    select(Event).order_by(Event.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()

            status_rows = (
                await session.execute(
                    select(
                        FollowRelation.status,
                        sa_func.count(),
                    ).group_by(FollowRelation.status)
                )
            ).all()

            skip_rows = (
                await session.execute(
                    select(
                        CopySkip.skip_reason,
                        sa_func.count(),
                    ).group_by(CopySkip.skip_reason)
                )
            ).all()

            hb_value = hb_row.value if hb_row is not None else None
            return {
                "heartbeat": hb_value,
                "last_event": (
                    None
                    if last_event is None
                    else (
                        last_event.event_type,
                        last_event.severity,
                        last_event.created_at,
                    )
                ),
                "follow": {str(s): int(c) for s, c in status_rows},
                "skips": {str(r): int(c) for r, c in skip_rows},
            }

    try:
        data = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Could not read trader status:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # --- Liveness verdict from the heartbeat row -------------------------
    hb = data["heartbeat"]
    hb_at_raw = hb.get("at") if isinstance(hb, dict) else None
    live_state = "[red]DOWN / no heartbeat[/red]"
    hb_display = "-"
    if hb_at_raw:
        hb_display = str(hb_at_raw)
        try:
            hb_dt = datetime.fromisoformat(str(hb_at_raw))
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=UTC)
            age = (_now() - hb_dt).total_seconds()
            if age <= _HEARTBEAT_STALE_SECONDS:
                live_state = f"[green]ALIVE[/green] ([dim]{int(age)}s ago[/dim])"
            else:
                live_state = (
                    f"[yellow]STALE[/yellow] "
                    f"([dim]{int(age)}s ago[/dim])"
                )
        except ValueError:
            live_state = "[yellow]heartbeat unparseable[/yellow]"

    # --- LIVE-TRADING banner — the real host env lock --------------------
    live_enabled = is_live_enabled()
    banner = (
        "[bold red]LIVE-TRADING: UNLOCKED[/bold red]"
        if live_enabled
        else "[bold green]LIVE-TRADING: LOCKED[/bold green]"
    )
    banner_detail = (
        "POLYECHO_LIVE_TRADING is ON — the engine MAY place real orders."
        if live_enabled
        else "POLYECHO_LIVE_TRADING is off — every order/redeem fails closed."
    )

    follow = data["follow"]
    active = follow.get("active", 0)
    paused = follow.get("paused", 0)
    other = sum(v for k, v in follow.items() if k not in ("active", "paused"))

    le = data["last_event"]
    last_event_line = (
        "[dim]no events recorded[/dim]"
        if le is None
        else f"{le[0]} [dim]({le[1]})[/dim] @ {_fmt_ts(le[2])}"
    )

    _console.print(
        Panel(
            f"{banner}\n[dim]{banner_detail}[/dim]\n\n"
            f"Engine (core)   {live_state}\n"
            f"Heartbeat row   {hb_display}\n"
            f"Last event      {last_event_line}\n\n"
            f"Follow relations  [green]{active} active[/green]  ·  "
            f"[yellow]{paused} paused[/yellow]"
            + (f"  ·  [dim]{other} other[/dim]" if other else ""),
            title="[dim]polyecho trader status[/dim]",
            border_style="red" if live_enabled else "cyan",
        )
    )

    skips = data["skips"]
    if not skips:
        _console.print(
            "[dim]No copy_skips recorded — nothing skipped yet.[/dim]"
        )
        return

    table = Table(
        title="Recent skips by reason (copy_skips)", title_style="bold"
    )
    table.add_column("SkipReason")
    table.add_column("Count", justify="right")
    for reason, count in sorted(
        skips.items(), key=lambda kv: kv[1], reverse=True
    ):
        table.add_row(reason, str(count))
    _console.print(table)


# ---------------------------------------------------------------------------
# pause-all / resume-all  (safe-write tier)
# ---------------------------------------------------------------------------
def _bulk_set_status(target: str) -> int:
    """Set every ``follow_relations`` row to *target*; return rows affected."""
    from sqlalchemy import update

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import FollowRelation

    async def _run() -> int:
        async with session_scope() as session:
            values: dict[str, Any] = {"status": target}
            if target == "paused":
                values["paused_at"] = _now()
            result = await session.execute(
                update(FollowRelation).values(**values)
            )
            return int(getattr(result, "rowcount", 0) or 0)

    return asyncio.run(_run())


@app.command(name="pause-all")
def pause_all() -> None:
    """Globally pause copy-trading (safe-write): all follows → ``paused``.

    The ``core`` engine reacts to the status change; it keeps running but
    copies nothing while every relation is paused.
    """
    try:
        n = _bulk_set_status("paused")
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]pause-all failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _console.print(
        Panel(
            f"[bold yellow]Paused[/bold yellow] {n} follow relation(s).\n"
            "[dim]The core engine keeps running but copies nothing while "
            "paused. Re-enable with `polyecho trader resume-all`.[/dim]",
            title="[dim]polyecho trader pause-all[/dim]",
            border_style="yellow",
        )
    )


def _confirm_live_or_die(yes: bool, action: str) -> None:
    """Require explicit confirmation for *action* when the live lock is ON.

    Mirrors the danger UX (``db migrate`` / ``enable-live``): when
    :func:`polyecho.shared.copytrading.is_live_enabled` is True, re-enabling
    copy-trading could resume placing **real** orders, so a deliberate
    ``--yes`` (or ``POLYECHO_TRADER_RESUME_YES=1``) is required.  When the
    live lock is OFF this is a harmless safe-write — no gate (pause-all is
    likewise never gated).  Non-TTY + no ``--yes`` ⇒ refuse (exit 2) rather
    than block forever.
    """
    from polyecho.shared.copytrading import is_live_enabled

    if not is_live_enabled():
        return
    if yes or os.environ.get("POLYECHO_TRADER_RESUME_YES") == "1":
        return
    if not sys.stdin.isatty():
        _console.print(
            f"[red]Refusing to {action} while LIVE-TRADING is "
            "UNLOCKED without confirmation.[/red] Pass [bold]--yes[/bold] "
            "or set [bold]POLYECHO_TRADER_RESUME_YES=1[/bold] "
            "(non-interactive / not a TTY)."
        )
        raise typer.Exit(code=2)
    _console.print(
        "[bold red]⚠  LIVE-TRADING is UNLOCKED[/bold red] — resuming will "
        "let the core engine place [bold red]real orders[/bold red]."
    )
    typer.confirm(f"Proceed to {action}?", abort=True)


@app.command(name="resume-all")
def resume_all(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the live-mode confirmation (also via "
        "POLYECHO_TRADER_RESUME_YES=1). Required when LIVE-TRADING is "
        "UNLOCKED and stdin is not a TTY.",
    ),
) -> None:
    """Globally resume copy-trading (safe-write): all follows → ``active``.

    When the host live lock (:func:`is_live_enabled`) is ON, this requires
    an explicit ``--yes``/confirm (mirrors the danger UX) because resuming
    could re-start placing real orders.  When the live lock is OFF it is an
    unguarded safe-write (pause-all is likewise never guarded).
    """
    _confirm_live_or_die(yes, "resume ALL follow relations")
    try:
        n = _bulk_set_status("active")
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]resume-all failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _console.print(
        Panel(
            f"[bold green]Resumed[/bold green] {n} follow relation(s).\n"
            "[dim]Copying resumes only if the host env var "
            "POLYECHO_LIVE_TRADING is also set for the core engine; "
            "otherwise orders still fail closed.[/dim]",
            title="[dim]polyecho trader resume-all[/dim]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# pause / resume  <leader_addr>  (safe-write tier, per-leader)
# ---------------------------------------------------------------------------
def _set_leader_status(leader_addr: str, target: str) -> int:
    """Set status for one leader's follow_relations; return rows affected."""
    from sqlalchemy import update

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import FollowRelation

    async def _run() -> int:
        async with session_scope() as session:
            values: dict[str, Any] = {"status": target}
            if target == "paused":
                values["paused_at"] = _now()
            result = await session.execute(
                update(FollowRelation)
                .where(FollowRelation.leader_address == leader_addr)
                .values(**values)
            )
            return int(getattr(result, "rowcount", 0) or 0)

    return asyncio.run(_run())


@app.command(name="pause")
def pause(
    leader_addr: str = typer.Argument(
        ..., help="Leader wallet address (0x…) to pause."
    ),
) -> None:
    """Pause copy-trading for one leader (safe-write)."""
    try:
        n = _set_leader_status(leader_addr, "paused")
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]pause failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if n == 0:
        _console.print(
            Panel(
                f"No follow relations for leader [bold]"
                f"{_short(leader_addr)}[/bold] — nothing changed.",
                title="[dim]polyecho trader pause[/dim]",
                border_style="yellow",
            )
        )
        return
    _console.print(
        Panel(
            f"[bold yellow]Paused[/bold yellow] {n} relation(s) for "
            f"leader [bold]{_short(leader_addr)}[/bold].",
            title="[dim]polyecho trader pause[/dim]",
            border_style="yellow",
        )
    )


@app.command(name="resume")
def resume(
    leader_addr: str = typer.Argument(
        ..., help="Leader wallet address (0x…) to resume."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the live-mode confirmation (also via "
        "POLYECHO_TRADER_RESUME_YES=1). Required when LIVE-TRADING is "
        "UNLOCKED and stdin is not a TTY.",
    ),
) -> None:
    """Resume copy-trading for one leader (safe-write).

    R15 #5: when the host live lock (:func:`is_live_enabled`) is ON,
    per-leader resume is gated the SAME as ``resume-all`` — otherwise an
    operator could bypass the live gate by resuming leaders one at a time.
    ``pause`` / ``pause-all`` remain ungated (pausing is always safe).
    """
    _confirm_live_or_die(yes, f"resume follow relations for {leader_addr}")
    try:
        n = _set_leader_status(leader_addr, "active")
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]resume failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if n == 0:
        _console.print(
            Panel(
                f"No follow relations for leader [bold]"
                f"{_short(leader_addr)}[/bold] — nothing changed.",
                title="[dim]polyecho trader resume[/dim]",
                border_style="yellow",
            )
        )
        return
    _console.print(
        Panel(
            f"[bold green]Resumed[/bold green] {n} relation(s) for "
            f"leader [bold]{_short(leader_addr)}[/bold].",
            title="[dim]polyecho trader resume[/dim]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# start  (handoff guidance + optional one-shot dry-run check)
# ---------------------------------------------------------------------------
@app.command(name="start")
def start(
    check: bool = typer.Option(
        False,
        "--check",
        help="Run a one-shot read-only dry-run check instead of "
        "printing handoff guidance only.",
    ),
) -> None:
    """Explain how to run the engine — this command never spawns the daemon.

    The trading engine runs as the long-lived ``core`` service
    (``python -m polyecho.core`` under ``docker compose``).  This CLI only
    controls it via the database; it deliberately does not fork a daemon
    that would hold keys.  ``--check`` performs a harmless read-only probe
    (live lock + follow counts) and is **always dry-run** unless the host
    env var ``POLYECHO_LIVE_TRADING`` is set for the engine.
    """
    from polyecho.shared.copytrading import is_live_enabled

    live_enabled = is_live_enabled()
    mode = (
        "[bold red]LIVE (POLYECHO_LIVE_TRADING is ON)[/bold red]"
        if live_enabled
        else "[bold green]DRY-RUN (live lock engaged)[/bold green]"
    )

    body = (
        "The trading engine is the [bold]core[/bold] service — it is NOT "
        "started by this CLI (the CLI never holds keys).\n\n"
        "Start it with:\n"
        "  [bold]docker compose up -d core[/bold]\n"
        "  [dim]# or, for local dev:[/dim]\n"
        "  [bold]python -m polyecho.core[/bold]\n\n"
        f"Effective mode: {mode}\n"
        "[dim]Real orders require BOTH active follow_relations AND the "
        "host env var POLYECHO_LIVE_TRADING=1 for the core process "
        "(the assert_live_allowed chokepoint). `trader enable-live` only "
        "records an audit ack — it is NOT the switch.[/dim]"
    )

    if check:
        try:
            from sqlalchemy import func as sa_func
            from sqlalchemy import select

            from polyecho.shared.db import session_scope
            from polyecho.shared.models import FollowRelation

            async def _probe() -> dict[str, int]:
                async with session_scope() as session:
                    rows = (
                        await session.execute(
                            select(
                                FollowRelation.status, sa_func.count()
                            ).group_by(FollowRelation.status)
                        )
                    ).all()
                    return {str(s): int(c) for s, c in rows}

            counts = asyncio.run(_probe())
        except Exception as exc:  # noqa: BLE001
            _console.print(f"[red]Dry-run check failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        body += (
            f"\n\n[dim]Dry-run check:[/dim] "
            f"{counts.get('active', 0)} active / "
            f"{counts.get('paused', 0)} paused follow relations. "
            "[dim](read-only; no orders placed)[/dim]"
        )

    _console.print(
        Panel(
            body,
            title="[dim]polyecho trader start[/dim]",
            border_style="red" if live_enabled else "cyan",
        )
    )


# ---------------------------------------------------------------------------
# enable-live  — DANGEROUS, CLI-only (PRD §5.7 dangerous tier)
# ---------------------------------------------------------------------------
@app.command(name="enable-live")
def enable_live(
    account: str = typer.Option(
        "",
        "--account",
        help="Account name or EOA whose passphrase authorises the ack. "
        "Defaults to the first account that has an encrypted key.",
    ),
) -> None:
    """DANGEROUS ack ritual — records intent to go live (does NOT move money).

    Ritual (PRD §5.7 dangerous tier, CLI-only — there is no web path):

    1. Print a blunt risk explanation.
    2. A visible 5-second countdown (Ctrl-C aborts).
    3. Prompt for the account passphrase (hidden) and *decrypt-check* it
       against the stored ``accounts.encrypted_private_key`` via
       :func:`polyecho.shared.crypto.decrypt_private_key`.  The passphrase
       and decrypted key are NEVER logged, echoed or returned.
    4. On success, write a ``settings`` row ``live_trading_ack`` =
       ``{by, at}`` as a tamper-visible audit trail.

    This is an **acknowledgement / audit step only**.  The binding
    live-money switch is the host environment variable
    ``POLYECHO_LIVE_TRADING`` read by
    :func:`polyecho.shared.copytrading.assert_live_allowed` in the core
    engine — intentionally outside any DB or web surface.
    """
    from sqlalchemy import or_, select

    from polyecho.shared.crypto import decrypt_private_key
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Account, Setting

    _console.print(
        Panel(
            "[bold red]⚠  DANGEROUS — enable-live acknowledgement[/bold red]\n\n"
            "This records that an operator [bold]intends[/bold] to run the "
            "engine with real funds. It does [bold]NOT[/bold] itself move "
            "money and is [bold]NOT[/bold] the live switch.\n\n"
            "Real trading additionally requires the operator to set the "
            "host env var [bold]POLYECHO_LIVE_TRADING=1[/bold] for the "
            "core service — that env var, read by [bold]assert_live_allowed"
            "[/bold], is the only true chokepoint. There is intentionally "
            "[bold]no web path[/bold] to this.\n\n"
            "Losses from live copy-trading are [bold red]real and "
            "irreversible[/bold red].",
            title="[bold red]polyecho trader enable-live[/bold red]",
            border_style="red",
        )
    )

    # Load the authorising account BEFORE the countdown so a misconfig
    # fails fast (and so an empty DB cannot be "acked").
    async def _load_account() -> tuple[str, bytes] | None:
        async with session_scope() as session:
            if account:
                row = (
                    await session.execute(
                        select(Account).where(
                            or_(
                                Account.name == account,
                                Account.eoa_address == account.lower(),
                            )
                        )
                    )
                ).scalar_one_or_none()
            else:
                row = (
                    await session.execute(
                        select(Account)
                        .where(Account.encrypted_private_key.is_not(None))
                        .order_by(Account.created_at)
                    )
                ).scalars().first()
            if row is None or row.encrypted_private_key is None:
                return None
            return (row.name, bytes(row.encrypted_private_key))

    try:
        loaded = asyncio.run(_load_account())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Could not load account:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if loaded is None:
        _console.print(
            "[red]No account with an encrypted key found — import one "
            "first with `polyecho account import`. Aborted.[/red]"
        )
        raise typer.Exit(code=1)

    acct_name, blob = loaded

    _console.print(
        f"[dim]Authorising via account[/dim] [bold]{acct_name}[/bold]"
        f"[dim]. Countdown then passphrase…[/dim]"
    )
    _countdown()

    # Hidden passphrase prompt — NEVER a CLI arg, NEVER echoed/logged.
    passphrase = typer.prompt("Account passphrase (hidden)", hide_input=True)
    try:
        # Decrypt-check ONLY: we verify the passphrase can unlock the
        # stored key, then immediately drop the plaintext. The key is
        # never assigned to a logged/returned name beyond this scope.
        decrypt_private_key(blob, passphrase)
    except Exception as exc:  # noqa: BLE001 - wrong passphrase / bad blob
        _console.print(
            "[red]Passphrase verification FAILED — no acknowledgement "
            "written. enable-live aborted.[/red]"
        )
        raise typer.Exit(code=1) from exc
    finally:
        # NOTE: this only rebinds the name — it does NOT scrub the secret
        # from memory. CPython `str` is immutable; the original passphrase
        # bytes remain resident until GC and are never zeroed. Best-effort
        # reference drop ONLY — not a memory-hygiene guarantee.
        passphrase = ""  # noqa: F841

    # Capture the ACK timestamp ONCE so the stored audit value and the
    # printed confirmation panel are byte-identical (R15 #2: two separate
    # _now() calls produced divergent timestamps in the row vs the panel).
    ack_ts = _now().isoformat()

    async def _write_ack() -> None:
        async with session_scope() as session:
            existing = (
                await session.execute(
                    select(Setting).where(Setting.key == _LIVE_ACK_KEY)
                )
            ).scalar_one_or_none()
            ack_value = {"by": acct_name, "at": ack_ts}
            if existing is None:
                session.add(Setting(key=_LIVE_ACK_KEY, value=ack_value))
            else:
                existing.value = ack_value

    try:
        asyncio.run(_write_ack())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Failed to write ack row:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _console.print(
        Panel(
            f"[bold green]ACK recorded[/bold green] by [bold]{acct_name}"
            f"[/bold] at {ack_ts}.\n\n"
            "[bold yellow]This is an AUDIT ACK ONLY — money has NOT been "
            "unlocked.[/bold yellow]\n"
            "To actually allow live orders you MUST set the host env var "
            "[bold]POLYECHO_LIVE_TRADING=1[/bold] for the core service and "
            "restart it. That env var (read by [bold]assert_live_allowed"
            "[/bold]) is the binding switch; this row is only a "
            "tamper-visible audit trail.\n"
            "[dim]Revert the ack with `polyecho trader disable-live`.[/dim]",
            title="[dim]polyecho trader enable-live[/dim]",
            border_style="green",
        )
    )


@app.command(name="disable-live")
def disable_live() -> None:
    """Clear the ``live_trading_ack`` audit row (does not touch the env lock).

    Removing the ack does not by itself stop live trading — that is still
    governed by the host ``POLYECHO_LIVE_TRADING`` env var — but it makes
    the operator's recorded intent explicit again.
    """
    from sqlalchemy import delete

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Setting

    async def _run() -> int:
        async with session_scope() as session:
            result = await session.execute(
                delete(Setting).where(Setting.key == _LIVE_ACK_KEY)
            )
            return int(getattr(result, "rowcount", 0) or 0)

    try:
        n = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]disable-live failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    msg = (
        "[bold yellow]Cleared[/bold yellow] the live_trading_ack row."
        if n
        else "[dim]No live_trading_ack row was present.[/dim]"
    )
    _console.print(
        Panel(
            f"{msg}\n[dim]Note: the binding live-money switch is the host "
            "env var POLYECHO_LIVE_TRADING — clearing this ack does not by "
            "itself disable it. Unset that env var and restart core to "
            "fully lock.[/dim]",
            title="[dim]polyecho trader disable-live[/dim]",
            border_style="yellow",
        )
    )


# ---------------------------------------------------------------------------
# skips  — query copy_skips (reason taxonomy)
# ---------------------------------------------------------------------------
@app.command(name="skips")
def skips(
    limit: int = typer.Option(
        20, "--limit", min=1, max=500, help="Max rows to show."
    ),
    reason: str = typer.Option(
        "",
        "--reason",
        help="Filter by a SkipReason value (e.g. data_unavailable, "
        "live_locked, position_cap).",
    ),
) -> None:
    """List recent ``copy_skips`` (the queryable skip/error taxonomy)."""
    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import CopySkip

    async def _run() -> list[tuple[Any, ...]]:
        async with session_scope() as session:
            stmt = select(CopySkip).order_by(CopySkip.created_at.desc())
            if reason:
                stmt = stmt.where(CopySkip.skip_reason == reason)
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                (
                    r.created_at,
                    r.skip_reason,
                    r.leader_address,
                    r.condition_id,
                    r.detail,
                )
                for r in rows
            ]

    try:
        records = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Could not query copy_skips:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not records:
        _console.print(
            Panel(
                "No matching skips."
                + (f" [dim](reason={reason})[/dim]" if reason else ""),
                title="[dim]polyecho trader skips[/dim]",
                border_style="green",
            )
        )
        return

    table = Table(
        title=(
            f"copy_skips (latest {len(records)}"
            + (f", reason={reason}" if reason else "")
            + ")"
        ),
        title_style="bold",
    )
    table.add_column("When")
    table.add_column("Reason")
    table.add_column("Leader")
    table.add_column("Condition")
    table.add_column("Detail")
    for created_at, skip_reason, leader, condition, detail in records:
        table.add_row(
            _fmt_ts(created_at),
            skip_reason,
            _short(leader),
            _short(condition),
            (detail or "")[:60],
        )
    _console.print(table)


# ---------------------------------------------------------------------------
# redeems  — list redeem_jobs
# ---------------------------------------------------------------------------
@app.command(name="redeems")
def redeems(
    status_filter: str = typer.Option(
        "",
        "--status",
        help="Filter by redeem_jobs.status "
        "(pending|submitted|confirmed|failed).",
    ),
    limit: int = typer.Option(
        50, "--limit", min=1, max=500, help="Max rows to show."
    ),
) -> None:
    """List ``redeem_jobs`` (durable claim-winnings queue, brief §9)."""
    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import RedeemJob

    async def _run() -> list[tuple[Any, ...]]:
        async with session_scope() as session:
            stmt = select(RedeemJob).order_by(RedeemJob.created_at.desc())
            if status_filter:
                stmt = stmt.where(RedeemJob.status == status_filter)
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                (
                    r.created_at,
                    r.status,
                    r.condition_id,
                    r.outcome_index,
                    r.is_neg_risk,
                    r.tx_hash,
                )
                for r in rows
            ]

    try:
        records = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Could not query redeem_jobs:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not records:
        _console.print(
            Panel(
                "No matching redeem jobs."
                + (
                    f" [dim](status={status_filter})[/dim]"
                    if status_filter
                    else ""
                ),
                title="[dim]polyecho trader redeems[/dim]",
                border_style="green",
            )
        )
        return

    table = Table(
        title=(
            f"redeem_jobs (latest {len(records)}"
            + (f", status={status_filter}" if status_filter else "")
            + ")"
        ),
        title_style="bold",
    )
    table.add_column("When")
    table.add_column("Status")
    table.add_column("Condition")
    table.add_column("Outcome", justify="right")
    table.add_column("NegRisk")
    table.add_column("Tx")
    for created_at, st, condition, outcome, neg, tx in records:
        st_style = {
            "confirmed": "green",
            "submitted": "cyan",
            "pending": "yellow",
            "failed": "red",
        }.get(str(st), "white")
        table.add_row(
            _fmt_ts(created_at),
            f"[{st_style}]{st}[/{st_style}]",
            _short(condition),
            "-" if outcome is None else str(outcome),
            "yes" if neg else ("no" if neg is not None else "-"),
            _short(tx),
        )
    _console.print(table)
