"""
polyecho.cli.db
===============
Database management sub-commands.

Commands
--------
* ``polyecho db migrate``    — apply 0000_init.sql against the configured DB
* ``polyecho db backup``     — pg_dump the configured DB to a host path (gzip)
* ``polyecho db reconcile``  — (stub, Phase 4) reconcile on-chain vs DB records
"""

from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from polyecho.cli._stub import not_implemented

app = typer.Typer(no_args_is_help=True)
_console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _migration_path() -> Path:
    """Locate ``infra/migrations/0000_init.sql`` relative to the repo root.

    Strategy: walk upward from this file until we find a directory that
    contains both ``infra/`` and ``packages/``, which is the repo root.
    Fallback to an env-var override ``POLYECHO_REPO_ROOT`` if set.
    """
    env_root = os.environ.get("POLYECHO_REPO_ROOT")
    if env_root:
        return Path(env_root) / "infra" / "migrations" / "0000_init.sql"

    # Walk up from __file__ looking for the repo root sentinel
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "infra").is_dir() and (parent / "packages").is_dir():
            return parent / "infra" / "migrations" / "0000_init.sql"

    raise FileNotFoundError(
        "Cannot locate repo root (directory containing both 'infra/' and "
        "'packages/'). Set POLYECHO_REPO_ROOT env var to override."
    )


def _redacted_db_url() -> str:
    """Return the configured async DB URL with the password masked.

    Uses SQLAlchemy's ``make_url`` so the redaction is structural (it never
    string-splits the raw DSN — avoids the leak class flagged in P3/C-cli),
    rendering the password as ``***``.
    """
    from sqlalchemy.engine import make_url

    from polyecho.shared.config import get_settings

    raw = get_settings().db_url.get_secret_value()
    return make_url(raw).render_as_string(hide_password=True)


def _libpq_dsn() -> str:
    """Return a plain ``postgresql://…`` libpq DSN for pg_dump.

    The SQLAlchemy URL is ``postgresql+asyncpg://…``; pg_dump wants a libpq
    URI, so the ``+asyncpg`` (or any ``+driver``) suffix is stripped from the
    scheme. The cleartext password is materialised here (single call site).
    """
    from polyecho.shared.config import get_settings

    url = get_settings().db_url.get_secret_value()
    scheme, sep, rest = url.partition("://")
    if sep and "+" in scheme:
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}{sep}{rest}" if sep else url


def _confirm_or_die(yes: bool, action: str) -> None:
    """Require explicit confirmation for a mutating action.

    ``--yes`` (or ``POLYECHO_DB_MIGRATE_YES=1``) bypasses the prompt. When
    neither is set and stdin is not a TTY (CI / piped), abort rather than
    block forever; when it IS a TTY, prompt interactively.
    """
    if yes or os.environ.get("POLYECHO_DB_MIGRATE_YES") == "1":
        return
    if not sys.stdin.isatty():
        _console.print(
            f"[red]Refusing to {action} without confirmation.[/red] "
            "Pass [bold]--yes[/bold] or set "
            "[bold]POLYECHO_DB_MIGRATE_YES=1[/bold] "
            "(non-interactive / not a TTY)."
        )
        raise typer.Exit(code=2)
    typer.confirm(f"Proceed to {action}?", abort=True)


# ---------------------------------------------------------------------------
# db migrate
# ---------------------------------------------------------------------------


@app.command(name="migrate")
def migrate(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the resolved (password-redacted) DB target and the "
            "SQL path, then exit WITHOUT executing anything.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the confirmation prompt (also via "
            "POLYECHO_DB_MIGRATE_YES=1). Required when stdin is not a TTY.",
        ),
    ] = False,
) -> None:
    """Apply ``infra/migrations/0000_init.sql`` to the configured database.

    The SQL file uses ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT
    EXISTS`` throughout, so re-running is safe (idempotent).

    Execution strategy
    ------------------
    ``0000_init.sql`` is a multi-statement script. asyncpg CANNOT run several
    commands through a prepared statement (the SQLAlchemy
    ``exec_driver_sql`` path), so this applies it via the **raw** asyncpg
    ``Connection.execute`` which DOES accept a simple-query script — exactly
    the path ``packages/shared/tests/test_schema_reflect.py`` exercises.
    """
    try:
        sql_path = _migration_path()
    except FileNotFoundError as exc:
        _console.print(f"[red]Migration file not found:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not sql_path.exists():
        _console.print(f"[red]Migration file missing:[/red] {sql_path}")
        raise typer.Exit(code=1)

    redacted = _redacted_db_url()
    _console.print(
        f"[dim]target[/dim] [cyan]{redacted}[/cyan]\n"
        f"[dim]sql   [/dim] {sql_path}"
    )

    if dry_run:
        _console.print(
            Panel(
                "[bold yellow]DRY RUN[/bold yellow] — nothing was executed.",
                title="[dim]polyecho db migrate --dry-run[/dim]",
                border_style="yellow",
            )
        )
        return

    _confirm_or_die(yes, f"apply {sql_path.name} to {redacted}")

    sql = sql_path.read_text(encoding="utf-8")

    async def _run() -> None:
        from polyecho.shared.db import get_engine

        engine = get_engine()
        # TRUE raw asyncpg connection: the simple-query protocol runs the
        # whole multi-statement idempotent script in one call. Mirrors
        # test_schema_reflect.py's proven path. The SQLAlchemy
        # exec_driver_sql / prepared path would fail with "cannot insert
        # multiple commands into a prepared statement".
        raw = await engine.raw_connection()
        try:
            asyncpg_conn = raw.driver_connection
            assert asyncpg_conn is not None  # an open raw conn always has one
            await asyncpg_conn.execute(sql)
        finally:
            raw.close()
        await engine.dispose()

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


# ---------------------------------------------------------------------------
# db backup
# ---------------------------------------------------------------------------


@app.command(name="backup")
def backup(
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Destination directory for the gzip'd dump. Defaults to "
            "/backups (bind-mount-friendly). Created if missing.",
        ),
    ] = Path("/backups"),
    gpg_recipient: Annotated[
        str | None,
        typer.Option(
            "--gpg-recipient",
            help="If set, encrypt the gzip'd dump for this GPG recipient "
            "(produces a .gpg artifact; the plaintext .gz is removed).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the resolved (password-redacted) DB target and the "
            "artifact path, then exit WITHOUT running pg_dump.",
        ),
    ] = False,
) -> None:
    """``pg_dump`` the configured database to a timestamped gzip artifact.

    Produces ``<out>/polyecho-<db>-<UTC-timestamp>.sql.gz`` (PRD §7.3 local
    backup). Optional ``--gpg-recipient`` adds an encrypted ``.gpg`` layer.
    Off-site replication (e.g. GCP Cloud Storage) is the **operator's**
    responsibility and is explicitly out of project scope (PRD note).

    Exits non-zero if ``pg_dump`` (or encryption) fails.
    """
    from sqlalchemy.engine import make_url

    dsn = _libpq_dsn()
    db_name = make_url(dsn).database or "polyecho"
    redacted = _redacted_db_url()
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact = out / f"polyecho-{db_name}-{ts}.sql.gz"

    _console.print(
        f"[dim]target[/dim] [cyan]{redacted}[/cyan]\n"
        f"[dim]out   [/dim] {artifact}"
        + (f"\n[dim]gpg   [/dim] {gpg_recipient}" if gpg_recipient else "")
    )

    if dry_run:
        _console.print(
            Panel(
                "[bold yellow]DRY RUN[/bold yellow] — pg_dump not executed.",
                title="[dim]polyecho db backup --dry-run[/dim]",
                border_style="yellow",
            )
        )
        return

    if shutil.which("pg_dump") is None:
        _console.print(
            "[red]Backup failed:[/red] pg_dump not found on PATH "
            "(run inside the cli container, which ships postgresql-client)."
        )
        raise typer.Exit(code=1)

    out.mkdir(parents=True, exist_ok=True)

    # `pg_dump <dsn> | gzip > artifact` (context7-verified canonical form),
    # implemented as a stream so pg_dump's stdout is compressed in-process
    # (a gzip.GzipFile has no OS fd to hand a child process directly).
    try:
        with gzip.open(artifact, "wb") as gz:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                ["pg_dump", "--dbname", dsn, "--no-owner", "--no-privileges"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdout is not None
            shutil.copyfileobj(proc.stdout, gz)
            _, stderr = proc.communicate()
        if proc.returncode != 0:
            artifact.unlink(missing_ok=True)
            err = (stderr or b"").decode("utf-8", "replace").strip()
            _console.print(
                f"[red]Backup failed (pg_dump):[/red] "
                f"{err or f'exit {proc.returncode}'}"
            )
            raise typer.Exit(code=1)
    except OSError as exc:
        artifact.unlink(missing_ok=True)
        _console.print(f"[red]Backup failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    final = artifact
    if gpg_recipient:
        if shutil.which("gpg") is None:
            artifact.unlink(missing_ok=True)
            _console.print(
                "[red]Backup failed:[/red] gpg not found on PATH "
                "(required for --gpg-recipient)."
            )
            raise typer.Exit(code=1)
        encrypted = artifact.with_suffix(artifact.suffix + ".gpg")
        try:
            subprocess.run(  # noqa: S603 — fixed argv, no shell
                [
                    "gpg",
                    "--batch",
                    "--yes",
                    "--trust-model",
                    "always",
                    "--encrypt",
                    "--recipient",
                    gpg_recipient,
                    "--output",
                    str(encrypted),
                    str(artifact),
                ],
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            encrypted.unlink(missing_ok=True)
            err = (exc.stderr or b"").decode("utf-8", "replace").strip()
            _console.print(f"[red]Backup failed (gpg):[/red] {err or exc}")
            raise typer.Exit(code=1) from exc
        artifact.unlink(missing_ok=True)
        final = encrypted

    _console.print(
        Panel(
            f"[bold green]Backup written[/bold green]\n"
            f"[dim]artifact[/dim] {final}",
            title="[dim]polyecho db backup[/dim]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# db reconcile (Phase 4 stub)
# ---------------------------------------------------------------------------


@app.command(name="reconcile")
def reconcile() -> None:
    """Reconcile on-chain positions vs DB records and report discrepancies."""
    not_implemented("db reconcile", 4)
