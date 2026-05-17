"""
polyecho.cli.doctor
===================
Phase 0 health-check command — ``polyecho doctor``.

Runs four checks and renders results in a Rich table:

1. **Config**      — settings load; required env vars present.
2. **Database**    — async ``SELECT 1`` via ``polyecho.shared.db``; reports latency.
3. **Polymarket**  — HTTP GET to CLOB health/time endpoint; reports reachable + status.
4. **Tailnet**     — ``tailscale status --json``; gracefully handles not-installed.

Exit code is non-zero if any *critical* check fails (Config, Database).
Polymarket and Tailnet failures are warnings only (non-zero but labelled).

Secrets are NEVER printed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from polyecho.shared.config import Settings, get_settings

_console = Console()

# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------

_OK = "[bold green]✓[/bold green]"
_FAIL = "[bold red]✗[/bold red]"
_WARN = "[bold yellow]⚠[/bold yellow]"


class _CheckResult:
    def __init__(
        self,
        name: str,
        *,
        ok: bool,
        detail: str,
        critical: bool = True,
    ) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail
        self.critical = critical

    @property
    def status_icon(self) -> str:
        if self.ok:
            return _OK
        return _FAIL if self.critical else _WARN


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_config() -> _CheckResult:
    """Verify that Settings loads and required variables are reachable."""
    try:
        settings: Settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        return _CheckResult("Config", ok=False, detail=f"Failed to load settings: {exc}")

    missing: list[str] = []

    if not settings.db_url:
        missing.append("POLYECHO_DB_URL")
    if not settings.polymarket_clob_url:
        missing.append("POLYECHO_POLYMARKET_CLOB_URL")
    if not settings.polymarket_data_api_url:
        missing.append("POLYECHO_POLYMARKET_DATA_API_URL")

    if missing:
        return _CheckResult(
            "Config",
            ok=False,
            detail=f"Missing required env vars: {', '.join(missing)}",
        )

    return _CheckResult(
        "Config",
        ok=True,
        detail=(
            f"DB driver={settings.db_url.split('+')[1].split('://')[0]}  "
            f"log_level={settings.log_level}"
        ),
    )


async def _async_check_db() -> _CheckResult:
    """Open an async DB connection and run SELECT 1; report latency."""
    from sqlalchemy import text  # local import to keep top-level fast

    from polyecho.shared.db import get_engine

    try:
        engine = get_engine()
        t0 = time.perf_counter()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        latency_ms = (time.perf_counter() - t0) * 1000
        return _CheckResult("Database", ok=True, detail=f"SELECT 1 OK  ({latency_ms:.0f} ms)")
    except Exception as exc:  # noqa: BLE001
        return _CheckResult("Database", ok=False, detail=f"Connection failed: {exc}")


def _check_db() -> _CheckResult:
    return asyncio.run(_async_check_db())


def _check_polymarket() -> _CheckResult:
    """HTTP GET to the CLOB time/health endpoint."""
    try:
        settings = get_settings()
        url = f"{settings.polymarket_clob_url.rstrip('/')}/time"
        t0 = time.perf_counter()
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.is_success:
            return _CheckResult(
                "Polymarket API",
                ok=True,
                detail=f"GET {url}  HTTP {resp.status_code}  ({latency_ms:.0f} ms)",
                critical=False,
            )
        return _CheckResult(
            "Polymarket API",
            ok=False,
            detail=f"GET {url}  HTTP {resp.status_code}  ({latency_ms:.0f} ms)",
            critical=False,
        )
    except httpx.ConnectError:
        return _CheckResult(
            "Polymarket API",
            ok=False,
            detail="Network unreachable — check internet connection.",
            critical=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _CheckResult(
            "Polymarket API",
            ok=False,
            detail=f"Unexpected error: {exc}",
            critical=False,
        )


def _check_tailnet() -> _CheckResult:
    """Run ``tailscale status --json`` and report tailnet connectivity."""
    try:
        result = subprocess.run(  # noqa: S603
            ["tailscale", "status", "--json"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return _CheckResult(
            "Tailnet",
            ok=False,
            detail="tailscale binary not found — not installed or not in PATH.",
            critical=False,
        )
    except subprocess.TimeoutExpired:
        return _CheckResult(
            "Tailnet",
            ok=False,
            detail="tailscale status timed out after 5 s.",
            critical=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _CheckResult(
            "Tailnet",
            ok=False,
            detail=f"Unexpected error running tailscale: {exc}",
            critical=False,
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        return _CheckResult(
            "Tailnet",
            ok=False,
            detail=f"tailscale exited {result.returncode}: {stderr or '(no stderr)'}",
            critical=False,
        )

    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _CheckResult(
            "Tailnet",
            ok=False,
            detail="Could not parse tailscale status JSON.",
            critical=False,
        )

    backend_state: str = data.get("BackendState", "unknown")
    self_node: dict[str, Any] = data.get("Self", {})
    hostname: str = self_node.get("HostName", "unknown")
    dns_name: str = self_node.get("DNSName", "").rstrip(".")

    if backend_state == "Running":
        return _CheckResult(
            "Tailnet",
            ok=True,
            detail=f"Running  hostname={hostname}  dns={dns_name or 'n/a'}",
            critical=False,
        )

    return _CheckResult(
        "Tailnet",
        ok=False,
        detail=f"BackendState={backend_state}  (not on tailnet)",
        critical=False,
    )


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------


def doctor() -> None:
    """Run system health checks: Config, Database, Polymarket API, Tailnet.

    Exit code is non-zero if any *critical* check (Config, Database) fails.
    """
    _console.print()
    _console.print("[bold]PolyEcho — System Health Check[/bold]")
    _console.print()

    results: list[_CheckResult] = [
        _check_config(),
        _check_db(),
        _check_polymarket(),
        _check_tailnet(),
    ]

    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    table.add_column("Check", style="bold", min_width=18)
    table.add_column("Status", justify="center", min_width=6)
    table.add_column("Detail")

    any_critical_failure = False
    for r in results:
        table.add_row(r.name, r.status_icon, r.detail)
        if not r.ok and r.critical:
            any_critical_failure = True

    _console.print(table)
    _console.print()

    if any_critical_failure:
        _console.print("[bold red]One or more critical checks failed.[/bold red]")
        raise typer.Exit(code=1)

    all_ok = all(r.ok for r in results)
    if all_ok:
        _console.print("[bold green]All checks passed.[/bold green]")
    else:
        _console.print(
            "[bold yellow]Non-critical checks have warnings — system is operational.[/bold yellow]"
        )
    raise typer.Exit(code=0)
