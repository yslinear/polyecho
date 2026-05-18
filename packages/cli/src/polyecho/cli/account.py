"""
polyecho.cli.account
====================
Account management sub-commands (Phase 3).

Commands
--------
* ``polyecho account detect <eoa>``   — derive 4 candidate wallets and rank
  them by on-chain evidence (positions ≫ balance ≫ activity).
* ``polyecho account import <eoa>``   — securely import + encrypt a private
  key (CLI only; prompts are hidden; the key is never a CLI arg and never
  logged/printed).
* ``polyecho account list``           — list registered accounts (never any
  key material).
* ``polyecho account balance <name>`` — best-effort USDC value for an account.

Security
--------
The private key and passphrase are read via hidden ``typer.prompt`` only,
encrypted in-process via :func:`polyecho.shared.crypto.encrypt_private_key`,
and written to ``accounts.encrypted_private_key``.  They are never echoed,
logged, returned, or placed on the command line.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
_console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _short(addr: str | None) -> str:
    """Abbreviate an address for table display (``0x1234…abcd``)."""
    if not addr:
        return "-"
    return addr if len(addr) <= 14 else f"{addr[:8]}…{addr[-6:]}"


def _confidence_style(confidence: str) -> str:
    return {
        "high": "bold green",
        "medium": "yellow",
        "low": "dim",
        "none": "red",
    }.get(confidence, "white")


# ---------------------------------------------------------------------------
# set-danger-passphrase  (P0-1 — operator provisioning; CLI is the ONLY writer)
# ---------------------------------------------------------------------------
@app.command(name="set-danger-passphrase")
def set_danger_passphrase_cmd() -> None:
    """設定（或輪替）操作員 danger 通關密語 — 僅限 CLI 寫入（P0-1）。

    這是 web 端 danger-tier 操作（手動賣出 / 緊急清倉）唯一驗證依據的祕密。
    web / DB-write 路徑永遠只「驗證」、絕不「設定」此密語。

    密語以隱藏輸入 + 二次確認方式讀取，**絕不**作為 CLI 參數（不會落入
    argv / shell history / log）。重新執行本指令會「輪替」既有密語。
    """
    from polyecho.shared.danger_passphrase import (
        is_danger_passphrase_provisioned,
        set_danger_passphrase,
    )

    # Hidden + confirmation prompt — NOT bound to a Typer Option, so it can
    # never appear in argv / shell history / logs.
    passphrase = typer.prompt(
        "Danger 通關密語（隱藏輸入）",
        hide_input=True,
        confirmation_prompt=True,
    )
    if not passphrase or not passphrase.strip():
        _console.print("[red]空白密語 — 已中止。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> bool:
        await set_danger_passphrase(passphrase)
        return await is_danger_passphrase_provisioned()

    try:
        provisioned = asyncio.run(_run())
    except ValueError as exc:
        _console.print(f"[red]密語無效：[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - clean CLI error surface
        _console.print(f"[red]設定失敗：[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        passphrase = ""  # noqa: F841 - drop plaintext reference promptly

    _console.print(
        Panel(
            "[bold green]Danger 通關密語已設定[/bold green]\n"
            f"已佈建 (is_danger_passphrase_provisioned) = "
            f"[bold]{'true' if provisioned else 'false'}[/bold]\n\n"
            "[yellow]⚠ 這是 web 端 danger-tier（手動賣出 / 緊急清倉）"
            "驗證所用的祕密。[/yellow]\n"
            "[yellow]⚠ web / DB-write 路徑只會驗證、永不設定此密語。[/yellow]\n"
            "[dim]再次執行本指令會輪替（覆蓋）既有密語。"
            "密語本身永不寫入 log 或顯示於畫面。[/dim]",
            title="[dim]polyecho account set-danger-passphrase[/dim]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------
@app.command(name="detect")
def detect(
    eoa: str = typer.Argument(..., help="EOA wallet address (0x…)"),
) -> None:
    """Detect the Polymarket signature type (Type 0–3) for an EOA address.

    Derives the four candidate funder addresses, queries the configured
    datasource for positions/activity on each, and prints a ranked table
    plus a recommendation.  Read-only — never writes the DB.
    """
    from polyecho.shared.accounts import (
        SIGNATURE_TYPE_DESCRIPTIONS,
        detect_signature_type,
    )
    from polyecho.shared.datasource import get_datasource

    try:
        ds = get_datasource()

        async def _run() -> Any:
            obj = ds
            aenter = getattr(ds, "__aenter__", None)
            if aenter is not None:
                obj = await aenter()
            try:
                return await detect_signature_type(eoa, obj)
            finally:
                aexit = getattr(ds, "__aexit__", None)
                if aexit is not None:
                    await aexit(None, None, None)

        result = asyncio.run(_run())
    except ValueError as exc:
        _console.print(f"[red]Invalid address:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(
        title=f"Signature-type detection for {result.eoa}",
        title_style="bold",
    )
    table.add_column("Type", justify="right")
    table.add_column("Name")
    table.add_column("Candidate address")
    table.add_column("Positions", justify="right")
    table.add_column("Activity", justify="right")
    table.add_column("USDC", justify="right")
    table.add_column("When to use")

    for ev in result.evidence:
        is_rec = ev.signature_type == result.recommended_type
        row_style = "bold green" if is_rec else ""
        bal = "-" if ev.usdc_balance is None else f"{ev.usdc_balance:,.2f}"
        # P0-5: type 2/3 have no trustworthy offline derivation — the shared
        # contract returns address="" for them.  Render an explicit zh-TW
        # hint instead of a blank/crashing cell.
        addr_cell = ev.address if ev.address else "[yellow]需顯式提供 funder[/yellow]"
        table.add_row(
            f"{ev.signature_type}{' ★' if is_rec else ''}",
            ev.name,
            addr_cell,
            str(ev.position_count),
            str(ev.activity_count),
            bal,
            SIGNATURE_TYPE_DESCRIPTIONS.get(ev.signature_type, ""),
            style=row_style,
        )

    _console.print(table)

    if result.recommended_type is None:
        _console.print(
            Panel(
                "[red]No positions or activity found for any candidate "
                "address.[/red]\nThe EOA may be unused on Polymarket, or the "
                "datasource has no data for it.",
                title="[dim]Recommendation[/dim]",
                border_style="red",
            )
        )
        return

    style = _confidence_style(result.confidence)
    _console.print(
        Panel(
            f"Recommended: [bold]Type {result.recommended_type}[/bold] "
            f"([bold]{result.evidence_for(result.recommended_type).name}[/bold])\n"
            f"Funder address: {result.recommended_address}\n"
            f"Confidence: [{style}]{result.confidence}[/{style}]\n\n"
            f"[dim]Next:[/dim] polyecho account import {result.eoa}",
            title="[dim]Recommendation[/dim]",
            border_style="green" if result.confidence == "high" else "yellow",
        )
    )


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------
@app.command(name="import")
def import_key(
    eoa: str = typer.Argument(
        ..., help="EOA wallet address whose private key to import."
    ),
    signature_type: int = typer.Option(
        2,
        "--signature-type",
        "-t",
        min=0,
        max=3,
        help="Polymarket signature type (0=EOA 1=POLY_PROXY "
        "2=POLY_GNOSIS_SAFE 3=POLY_1271). Run `account detect` first.",
    ),
    name: str = typer.Option(
        "",
        "--name",
        help="Friendly account name (defaults to the EOA address).",
    ),
    funder: str = typer.Option(
        "",
        "--funder",
        help="Explicit, on-chain-verified funder address (0x…). REQUIRED "
        "for signature-type 2 (POLY_GNOSIS_SAFE) and 3 (POLY_1271): these "
        "have NO trustworthy offline derivation — see runbook §2.3. For "
        "type 0/1 the verified derived funder is used and this is ignored.",
    ),
) -> None:
    """Securely import a private key for an EOA account (CLI only — never via Web UI).

    Both the **passphrase** and the **private key** are read via standalone
    hidden ``typer.prompt`` calls — NEVER Typer Options.  This is the P3
    hardening: a ``typer.Option(..., prompt=...)`` is still bindable as a
    CLI flag, so the secret could land in ``argv`` / shell history /
    process listings / CI logs.  A bare
    ``typer.prompt(hide_input=True, confirmation_prompt=True)`` has no
    corresponding flag and therefore cannot (matches
    ``account set-danger-passphrase``).

    The private key is encrypted in-process with
    ``shared.crypto.encrypt_private_key`` and the ``accounts`` row is
    upserted with ``signature_type`` / ``funder_address`` /
    ``encrypted_private_key`` and ``status`` transitioned
    ``pending_key_import`` → ``ready``.  The key/passphrase are never
    echoed, logged, or printed.
    """
    from eth_utils.address import is_address
    from sqlalchemy import select

    from polyecho.shared.accounts import (
        SIGNATURE_TYPE_NAMES,
        derive_wallets,
    )
    from polyecho.shared.crypto import encrypt_private_key
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Account

    try:
        wallets = derive_wallets(eoa)
    except ValueError as exc:
        _console.print(f"[red]Invalid address:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    eoa_lower = wallets[0]
    assert eoa_lower is not None  # type 0 is always exact (never None)

    # P0-5: consume the verified derive_wallets contract.
    #   type 0/1 → verified derived funder (exact / brief §9 CREATE2).
    #   type 2/3 → derive_wallets returns None (no trustworthy offline
    #              derivation); operator MUST supply an on-chain-verified
    #              --funder.  We refuse loudly rather than persist a
    #              fabricated address (runbook §2.3).
    derived = wallets[signature_type]
    if derived is not None:
        # Types 0/1: use the verified derived value; ignore any --funder.
        funder_addr = derived
    else:
        explicit = funder.strip()
        if not explicit:
            _console.print(
                Panel(
                    f"[red]簽章型別 {signature_type} "
                    f"({SIGNATURE_TYPE_NAMES.get(signature_type, '?')}) "
                    f"沒有可信的離線推導。[/red]\n"
                    "type 2 (POLY_GNOSIS_SAFE) 須由 Safe factory RPC "
                    "computeProxyAddress(eoa) 解析；type 3 (POLY_1271) "
                    "沒有任何參考實作 — 兩者都必須由操作員提供經"
                    "[bold]鏈上驗證[/bold]的 funder。\n\n"
                    "[bold]請以 --funder 0x… 顯式提供並於鏈上核對"
                    "（見 runbook §2.3）。[/bold]\n"
                    "[dim]系統拒絕寫入臆測位址 — 寫入錯誤 funder "
                    "比拒絕更危險。[/dim]",
                    title="[red]缺少 funder — 已中止[/red]",
                    border_style="red",
                )
            )
            raise typer.Exit(code=1)
        if not is_address(explicit):
            _console.print(
                f"[red]--funder 不是有效的 EVM 位址：[/red] {explicit!r}"
            )
            raise typer.Exit(code=1)
        funder_addr = explicit.lower()

    account_name = name or eoa_lower

    # Hidden + confirmation passphrase prompt — NOT a Typer Option, so it
    # can never appear in argv / shell history / logs (P3 hardening; this
    # matches `account set-danger-passphrase`).
    passphrase = typer.prompt(
        "Encryption passphrase (hidden)",
        hide_input=True,
        confirmation_prompt=True,
    )
    if not passphrase or not passphrase.strip():
        _console.print("[red]Empty passphrase — aborted.[/red]")
        raise typer.Exit(code=1)

    # Hidden private-key prompt — NEVER a CLI argument, NEVER logged/echoed.
    private_key = typer.prompt("Private key (hidden)", hide_input=True)
    if not private_key or not private_key.strip():
        _console.print("[red]Empty private key — aborted.[/red]")
        raise typer.Exit(code=1)

    try:
        encrypted = encrypt_private_key(private_key.strip(), passphrase)
    finally:
        # Best-effort: drop the plaintext references promptly.
        private_key = ""  # noqa: F841
        passphrase = ""  # noqa: F841

    async def _run() -> str:
        async with session_scope() as session:
            existing = (
                await session.execute(
                    select(Account).where(Account.eoa_address == eoa_lower)
                )
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    Account(
                        name=account_name,
                        eoa_address=eoa_lower,
                        funder_address=funder_addr,
                        signature_type=signature_type,
                        encrypted_private_key=encrypted,
                        status="ready",
                    )
                )
                return "created"

            existing.name = account_name
            existing.funder_address = funder_addr
            existing.signature_type = signature_type
            existing.encrypted_private_key = encrypted
            existing.status = "ready"
            return "updated"

    try:
        action = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        _console.print(f"[red]Import failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _console.print(
        Panel(
            f"[bold green]{action}[/bold green] account "
            f"[bold]{account_name}[/bold]\n"
            f"EOA       {eoa_lower}\n"
            f"Funder    {funder_addr}\n"
            f"Sig type  {signature_type} "
            f"({SIGNATURE_TYPE_NAMES.get(signature_type, '?')})\n"
            f"Status    pending_key_import → [bold green]ready[/bold green]\n"
            f"[dim]Private key encrypted (AES-256-GCM) and stored. "
            f"Key material is never logged or displayed.[/dim]",
            title="[dim]polyecho account import[/dim]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@app.command(name="list")
def list_accounts() -> None:
    """List all registered PolyEcho accounts.

    Never displays key material — only metadata and whether an encrypted key
    is present.
    """
    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Account

    async def _run() -> list[Any]:
        async with session_scope() as session:
            rows = (
                await session.execute(select(Account).order_by(Account.created_at))
            ).scalars().all()
            # Detach plain values so the session can close before rendering.
            return [
                (
                    a.name,
                    a.eoa_address,
                    a.funder_address,
                    a.signature_type,
                    a.status,
                    a.encrypted_private_key is not None,
                )
                for a in rows
            ]

    try:
        accounts = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Could not load accounts:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not accounts:
        _console.print(
            Panel(
                "No accounts registered yet.\n"
                "[dim]Run `polyecho account detect <eoa>` then "
                "`polyecho account import <eoa>`.[/dim]",
                title="[dim]polyecho account list[/dim]",
                border_style="yellow",
            )
        )
        return

    table = Table(title="PolyEcho accounts", title_style="bold")
    table.add_column("Name")
    table.add_column("EOA")
    table.add_column("Funder")
    table.add_column("Sig", justify="right")
    table.add_column("Status")
    table.add_column("Key")

    for name, eoa_address, funder, sig_type, status, has_key in accounts:
        status_style = {
            "ready": "green",
            "pending_key_import": "yellow",
            "disabled": "dim",
        }.get(status, "white")
        table.add_row(
            name,
            _short(eoa_address),
            _short(funder),
            str(sig_type),
            f"[{status_style}]{status}[/{status_style}]",
            "[green]✓ encrypted[/green]" if has_key else "[red]—[/red]",
        )

    _console.print(table)


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------
@app.command(name="balance")
def balance(
    name: str = typer.Argument(..., help="Account name or EOA address."),
) -> None:
    """Query the USDC value and position summary for an account (best-effort).

    Resolves *name* (account name or EOA) to a row, then sums open-position
    value via the configured datasource and probes USDC balance best-effort.
    """
    from sqlalchemy import or_, select

    from polyecho.shared.accounts import fetch_usdc_balance
    from polyecho.shared.datasource import get_datasource
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Account

    name_lower = name.lower()

    # signature_type is NULLable on the accounts model (3-way schema
    # alignment: 0000_init.sql has no NOT NULL) → int | None here.
    async def _load() -> tuple[str, str | None, int | None] | None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Account).where(
                        or_(
                            Account.name == name,
                            Account.eoa_address == name_lower,
                        )
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return (row.name, row.funder_address or row.eoa_address, row.signature_type)

    try:
        loaded = asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Could not load account:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if loaded is None:
        _console.print(f"[red]Account not found:[/red] {name}")
        raise typer.Exit(code=1)

    acct_name, funder, sig_type = loaded
    funder_addr = (funder or "").lower()

    async def _balance() -> tuple[int, float, float | None]:
        ds = get_datasource()
        obj = ds
        aenter = getattr(ds, "__aenter__", None)
        if aenter is not None:
            obj = await aenter()
        try:
            try:
                positions = await obj.get_positions(funder_addr)
            except Exception:  # noqa: BLE001 - best-effort
                positions = []
            pos_value = sum(p.current_value for p in positions)
            usdc = await fetch_usdc_balance(funder_addr)
            return len(positions), pos_value, usdc
        finally:
            aexit = getattr(ds, "__aexit__", None)
            if aexit is not None:
                await aexit(None, None, None)

    try:
        pos_count, pos_value, usdc = asyncio.run(_balance())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Balance query failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    usdc_line = (
        "[dim]unavailable (best-effort)[/dim]"
        if usdc is None
        else f"[bold]{usdc:,.2f}[/bold] USDC"
    )
    _console.print(
        Panel(
            f"Account   [bold]{acct_name}[/bold]\n"
            f"Funder    {funder_addr}\n"
            f"Sig type  {sig_type}\n"
            f"USDC      {usdc_line}\n"
            f"Positions {pos_count} open  "
            f"(mark-to-market ≈ [bold]{pos_value:,.2f}[/bold] USD)",
            title="[dim]polyecho account balance[/dim]",
            border_style="cyan",
        )
    )
