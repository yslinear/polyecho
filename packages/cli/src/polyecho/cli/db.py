"""
polyecho.cli.db
===============
Database management sub-commands (Phase 0 / Phase 1).

Commands
--------
* ``polyecho db migrate``    — apply pending schema migrations
* ``polyecho db backup``     — trigger an immediate DB backup
* ``polyecho db reconcile``  — reconcile on-chain state vs DB records
"""

from __future__ import annotations

import typer

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)


@app.command(name="migrate")
def migrate() -> None:
    """Apply pending Drizzle schema migrations to the database."""
    not_implemented("db migrate", 0)


@app.command(name="backup")
def backup() -> None:
    """Trigger an immediate database backup to local disk and GCP Cloud Storage."""
    not_implemented("db backup", 1)


@app.command(name="reconcile")
def reconcile() -> None:
    """Reconcile on-chain positions vs DB records and report discrepancies."""
    not_implemented("db reconcile", 4)
