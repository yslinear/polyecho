"""
polyecho.cli.account
====================
Account management sub-commands (Phase 3).

Commands
--------
* ``polyecho account detect <eoa>``   — detect wallet signature type
* ``polyecho account import <eoa>``   — securely import private key
* ``polyecho account list``            — list registered accounts
* ``polyecho account balance <name>`` — query account balance
"""

from __future__ import annotations

import typer

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)

_PHASE = 3


@app.command(name="detect")
def detect(
    eoa: str = typer.Argument(..., help="EOA wallet address (0x…)"),
) -> None:
    """Detect the Polymarket signature type (Type 0–3) for an EOA address."""
    not_implemented(f"account detect {eoa}", _PHASE)


@app.command(name="import")
def import_key(
    eoa: str = typer.Argument(..., help="EOA wallet address whose private key to import."),
) -> None:
    """Securely import a private key for an EOA account (CLI only — never via Web UI)."""
    not_implemented(f"account import {eoa}", _PHASE)


@app.command(name="list")
def list_accounts() -> None:
    """List all registered PolyEcho accounts."""
    not_implemented("account list", _PHASE)


@app.command(name="balance")
def balance(
    name: str = typer.Argument(..., help="Account name or EOA address."),
) -> None:
    """Query the USDC balance and authorisation status for an account."""
    not_implemented(f"account balance {name}", _PHASE)
