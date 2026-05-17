"""
polyecho.cli.alerts
===================
Push-event (alerts) viewer sub-command (Phase-5 Wave-2).

Commands
--------
* ``polyecho alerts list`` — show recent rows from the ``events`` table
  (the push-event log :mod:`polyecho.shared.events` writes and
  ``web-push-sender`` consumes).  Read-only.

Output is a clean aligned table:

* ``Created`` — ``events.created_at`` rendered in ``settings.tz``.
* ``Severity`` — colourised info / warning / critical.
* ``EventType`` — the ``events.event_type`` value.
* ``Summary`` — a short, truncated view of ``payload.data``.
* ``Delivered`` — ✓ if ``pushed_at`` is set, ✗ otherwise.

Event payloads are non-secret, but ``data`` blobs can be large so the
summary is truncated for display.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
_console = Console()

#: Accepted ``--severity`` filter values (mirrors shared ``Severity``).
_SEVERITIES = ("info", "warning", "critical")

#: Max characters of the payload.data summary shown in the table.
_SUMMARY_MAX = 60


def _tzinfo() -> ZoneInfo:
    """Resolve ``settings.tz`` to a ZoneInfo, falling back to UTC."""
    from polyecho.shared.config import get_settings

    name = get_settings().tz
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _fmt_local(value: Any, tz: ZoneInfo) -> str:
    """Render a timestamp in *tz* (accepts datetime or ISO string)."""
    if value is None:
        return "-"
    dt = value
    if not isinstance(dt, datetime):
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _summarise(payload: Any) -> str:
    """Build a short, truncated summary of ``payload['data']``.

    ``events.payload`` is the ``{"data": ..., "push": ...}`` envelope.
    Only ``data`` is summarised; large blobs are truncated to keep the
    table readable.  Returns ``-`` when there is nothing useful.
    """
    if not isinstance(payload, dict):
        return "-"
    data = payload.get("data")
    if data is None:
        return "-"
    if isinstance(data, dict):
        if not data:
            return "-"
        text = ", ".join(f"{k}={data[k]}" for k in list(data)[:4])
    else:
        text = str(data)
    text = " ".join(text.split())
    return text if len(text) <= _SUMMARY_MAX else text[: _SUMMARY_MAX - 1] + "…"


@app.command(name="list")
def list_alerts(
    limit: int = typer.Option(
        20,
        "--limit",
        "-n",
        min=1,
        max=500,
        help="Max number of recent events to show.",
    ),
    severity: str = typer.Option(
        "",
        "--severity",
        help="Filter by severity (info | warning | critical).",
    ),
    unpushed: bool = typer.Option(
        False,
        "--unpushed",
        help="Only show events not yet delivered (pushed_at IS NULL).",
    ),
) -> None:
    """List recent ``events`` (the push-event log), newest first."""
    sev = severity.strip().lower()
    if sev and sev not in _SEVERITIES:
        _console.print(
            f"[red]Invalid --severity {severity!r}[/red] "
            f"[dim](expected one of: {', '.join(_SEVERITIES)})[/dim]"
        )
        raise typer.Exit(code=2)

    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Event

    async def _run() -> list[tuple[Any, ...]]:
        async with session_scope() as session:
            stmt = select(Event).order_by(Event.created_at.desc())
            if sev:
                stmt = stmt.where(Event.severity == sev)
            if unpushed:
                stmt = stmt.where(Event.pushed_at.is_(None))
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                (
                    r.created_at,
                    r.severity,
                    r.event_type,
                    r.payload,
                    r.pushed_at,
                )
                for r in rows
            ]

    try:
        records = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Could not query events:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    filters = []
    if sev:
        filters.append(f"severity={sev}")
    if unpushed:
        filters.append("unpushed")
    filter_note = f" [dim]({', '.join(filters)})[/dim]" if filters else ""

    if not records:
        _console.print(
            Panel(
                "No events recorded." + filter_note,
                title="[dim]polyecho alerts[/dim]",
                border_style="green",
            )
        )
        return

    tz = _tzinfo()
    table = Table(
        title=(
            f"events (latest {len(records)}"
            + (f", {', '.join(filters)}" if filters else "")
            + ")"
        ),
        title_style="bold",
    )
    table.add_column("Created")
    table.add_column("Severity")
    table.add_column("EventType")
    table.add_column("Summary")
    table.add_column("Delivered", justify="center")

    for created_at, severity_val, event_type, payload, pushed_at in records:
        sev_style = {
            "critical": "bold red",
            "warning": "yellow",
            "info": "cyan",
        }.get(str(severity_val), "white")
        delivered = (
            "[green]✓[/green]" if pushed_at is not None else "[red]✗[/red]"
        )
        table.add_row(
            _fmt_local(created_at, tz),
            f"[{sev_style}]{severity_val}[/{sev_style}]",
            str(event_type),
            _summarise(payload),
            delivered,
        )
    _console.print(table)
