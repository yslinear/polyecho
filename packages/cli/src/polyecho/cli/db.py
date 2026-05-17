"""
polyecho.cli.db
===============
Database management sub-commands.

Commands
--------
* ``polyecho db migrate``    — apply 0000_init.sql against the configured DB
* ``polyecho db backup``     — (stub, Phase 1) trigger an immediate DB backup
* ``polyecho db reconcile``  — (stub, Phase 4) reconcile on-chain vs DB records
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)
_console = Console()


def _migration_path() -> Path:
    """Locate ``infra/migrations/0000_init.sql`` relative to the repo root.

    Strategy: walk upward from this file until we find a directory that
    contains both ``infra/`` and ``packages/``, which is the repo root.
    Fallback to an env-var override ``POLYECHO_REPO_ROOT`` if set.
    """
    import os

    env_root = os.environ.get("POLYECHO_REPO_ROOT")
    if env_root:
        return Path(env_root) / "infra" / "migrations" / "0000_init.sql"

    # Walk up from __file__ looking for the repo root sentinel
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "infra").is_dir() and (parent / "packages").is_dir():
            return parent / "infra" / "migrations" / "0000_init.sql"

    raise FileNotFoundError(
        "Cannot locate repo root (directory containing both 'infra/' and 'packages/'). "
        "Set POLYECHO_REPO_ROOT env var to override."
    )


@app.command(name="migrate")
def migrate() -> None:
    """Apply ``infra/migrations/0000_init.sql`` to the configured database.

    The SQL file uses ``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT
    EXISTS`` throughout, so re-running is safe (idempotent).

    Execution strategy
    ------------------
    asyncpg can execute a multi-statement SQL script via a raw connection's
    ``execute()`` method — it splits on ``;`` internally and runs each
    statement sequentially in a single round-trip.  SQLAlchemy's
    ``AsyncConnection.exec_driver_sql()`` surfaces this directly.
    """
    try:
        sql_path = _migration_path()
    except FileNotFoundError as exc:
        _console.print(f"[red]Migration file not found:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not sql_path.exists():
        _console.print(f"[red]Migration file missing:[/red] {sql_path}")
        raise typer.Exit(code=1)

    sql = sql_path.read_text(encoding="utf-8")

    async def _run() -> None:
        from polyecho.shared.db import get_engine

        engine = get_engine()
        # Use a raw asyncpg connection so we can pass the full multi-statement
        # script in one call.  asyncpg.Connection.execute() handles semicolon-
        # separated statements natively.
        async with engine.connect() as conn:
            # exec_driver_sql forwards the string directly to asyncpg,
            # bypassing SQLAlchemy's statement parsing.
            await conn.exec_driver_sql(sql)
            await conn.commit()

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]Migration failed:[/red] {exc}")
        sys.exit(1)

    _console.print(
        Panel(
            f"[bold green]Applied[/bold green]  {sql_path.name}\n"
            f"[dim]path[/dim] {sql_path}",
            title="[dim]polyecho db migrate[/dim]",
            border_style="green",
        )
    )


@app.command(name="backup")
def backup() -> None:
    """Trigger an immediate database backup to local disk and GCP Cloud Storage."""
    not_implemented("db backup", 1)


@app.command(name="reconcile")
def reconcile() -> None:
    """Reconcile on-chain positions vs DB records and report discrepancies."""
    not_implemented("db reconcile", 4)
