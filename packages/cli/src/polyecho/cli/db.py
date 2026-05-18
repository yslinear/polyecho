"""
polyecho.cli.db
===============
Database management sub-commands.

Commands
--------
* ``polyecho db migrate``    — apply 0000_init.sql against the configured DB
* ``polyecho db backup``     — pg_dump the configured DB to a host path (gzip)
* ``polyecho db reconcile``  — on-chain ↔ DB reconciliation; invokes the
  frozen :mod:`polyecho.core.onchain_reconcile` (read-only single pass).
"""

from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

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
            # Runtime guard — NOT `assert` (stripped under python -O /
            # PYTHONOPTIMIZE, which would let a None slip through to an
            # opaque AttributeError instead of this clear failure).
            if asyncpg_conn is None:
                raise RuntimeError(
                    "raw asyncpg connection unexpectedly has no "
                    "driver_connection (cannot apply migration)"
                )
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
    #
    # Deadlock-safe (R14 #3): we consume stdout in-process while pg_dump
    # runs.  If stderr were a PIPE that nobody drains, pg_dump would block
    # once the OS stderr pipe buffer fills (a large/verbose dump) → the
    # `copyfileobj` reader and the child deadlock forever (context7 /
    # CPython subprocess docs: "wait() can deadlock ... use communicate()").
    # We canNOT call communicate() here because stdout is already being
    # streamed to gzip out-of-band.  Instead stderr is redirected to a
    # real OS-fd temp file (a SpooledTemporaryFile with a generous
    # rollover) — that fd has no fixed pipe-buffer ceiling, so pg_dump
    # never blocks on it; we read it back only on failure.
    try:
        with (
            gzip.open(artifact, "wb") as gz,
            tempfile.SpooledTemporaryFile(
                max_size=1 << 20, mode="w+b"
            ) as errfile,
        ):
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                ["pg_dump", "--dbname", dsn, "--no-owner", "--no-privileges"],
                stdout=subprocess.PIPE,
                stderr=errfile,
            )
            if proc.stdout is None:  # never None with stdout=PIPE
                proc.kill()
                proc.wait()
                raise RuntimeError("pg_dump stdout pipe was not created")
            with proc.stdout:
                shutil.copyfileobj(proc.stdout, gz)
            returncode = proc.wait()
            errfile.seek(0)
            stderr = errfile.read()
        if returncode != 0:
            artifact.unlink(missing_ok=True)
            err = (stderr or b"").decode("utf-8", "replace").strip()
            _console.print(
                f"[red]Backup failed (pg_dump):[/red] "
                f"{err or f'exit {returncode}'}"
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
# db reconcile — invoke the frozen polyecho.core.onchain_reconcile
# ---------------------------------------------------------------------------


def _render_reconcile_report(report: object) -> None:
    """Render a :class:`ReconcileReport`-shaped object to a Rich panel.

    The frozen contract (C-reconcile) returns a ``ReconcileReport``.  We do
    not import its type (avoids an import-time coupling on a parallel
    package); we render it duck-typed: a ``summary``/``__str__`` for the
    human line and an optional ``drift_count`` / ``drifts`` for detail.
    """
    summary = getattr(report, "summary", None)
    if callable(summary):
        summary = summary()
    body = str(summary) if summary is not None else str(report)

    drift_count = getattr(report, "drift_count", None)
    if drift_count is None:
        drifts = getattr(report, "drifts", None)
        drift_count = len(drifts) if drifts is not None else None

    has_drift = bool(drift_count) if drift_count is not None else None
    border = "yellow" if has_drift else "green"
    _console.print(
        Panel(
            body,
            title="[dim]polyecho db reconcile[/dim]",
            border_style=border,
        )
    )


@app.command(name="reconcile")
def reconcile(
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero when drift is detected (default: drift is "
            "REPORTED, not a failure — only a real reconcile error fails).",
        ),
    ] = False,
) -> None:
    """Reconcile on-chain positions/balances vs DB records (read-only).

    Invokes the FROZEN ``polyecho.core.onchain_reconcile`` contract
    (delivered by the parallel C-reconcile agent): a single-pass,
    READ-ONLY comparison of our DB ``my_trades``/positions against the
    on-chain ERC-1155/USDC state.  No live-lock is needed (it never
    writes / trades).

    Wiring
    ------
    Prefer importing and awaiting
    :func:`polyecho.core.onchain_reconcile.run_reconcile` (same in-process
    style every other ``db``/``trader`` command uses — ``asyncio.run`` over
    a shared/core coroutine).  If that import is unavailable at call time,
    fall back to ``python -m polyecho.core.onchain_reconcile --once`` (the
    frozen module entry-point: exit 0 on success/no-drift, non-zero on a
    real error) so the operator path still works.

    Exit code
    ---------
    Drift detected is **reported**, not a failure — exit stays 0 unless
    ``--strict`` is passed.  A genuine reconcile *error* (RPC down, DB
    unreachable, the frozen entry-point itself failing) always exits
    non-zero.
    """
    # Resolve the FROZEN contract dynamically via importlib: the module is
    # delivered by a *parallel* package (C-reconcile), so a static
    # ``from polyecho.core.onchain_reconcile import …`` would couple this
    # package's type-check / import graph to one that may not be present at
    # check time.  importlib keeps the coupling purely runtime; an absent
    # module (ImportError) or a module missing ``run_reconcile``
    # (AttributeError) both fall back to the ``python -m`` entry-point.
    import importlib

    try:
        _mod = importlib.import_module("polyecho.core.onchain_reconcile")
        run_reconcile = _mod.run_reconcile
    except (ImportError, AttributeError):
        _reconcile_via_subprocess(strict=strict)
        return

    async def _run() -> object:
        return await run_reconcile()

    try:
        report = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Reconcile failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_reconcile_report(report)

    if strict:
        drift_count = getattr(report, "drift_count", None)
        if drift_count is None:
            drifts = getattr(report, "drifts", None)
            drift_count = len(drifts) if drifts is not None else 0
        if drift_count:
            _console.print(
                f"[yellow]--strict: {drift_count} drift(s) detected — "
                "exiting non-zero.[/yellow]"
            )
            raise typer.Exit(code=1)


def _reconcile_via_subprocess(*, strict: bool) -> None:
    """Fallback path: ``python -m polyecho.core.onchain_reconcile --once``.

    Used only when ``run_reconcile`` cannot be imported (an undesirable
    import-time coupling on the parallel core package).  The frozen module
    entry-point is ``--once`` (single pass), exit 0 on success/no-drift and
    non-zero on a real error.  ``--strict`` is forwarded so the same
    drift-fails-or-not policy holds for the subprocess path.
    """
    cmd = [sys.executable, "-m", "polyecho.core.onchain_reconcile", "--once"]
    if strict:
        cmd.append("--strict")
    _console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, check=False
        )
    except OSError as exc:
        _console.print(f"[red]Reconcile failed to launch:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if completed.returncode != 0:
        _console.print(
            f"[red]Reconcile error[/red] (onchain_reconcile exited "
            f"{completed.returncode})."
        )
        raise typer.Exit(code=completed.returncode)

    _console.print(
        Panel(
            "[bold green]Reconcile complete.[/bold green]\n"
            "[dim]Ran via `python -m polyecho.core.onchain_reconcile "
            "--once` (frozen entry-point). See its structured log for the "
            "ReconcileReport detail.[/dim]",
            title="[dim]polyecho db reconcile[/dim]",
            border_style="green",
        )
    )
