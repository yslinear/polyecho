"""
polyecho.cli.status
===================
``polyecho status`` — the aggregated system-status command (CLI parity
with the web ``/status`` page).

Calls the frozen aggregator :func:`polyecho.shared.health.get_status`
(an ``async`` coroutine that reads every worker *heartbeat* + the
``integration_status`` ``settings`` row and returns a typed
:class:`~polyecho.shared.health.StatusSnapshot`) and renders it as two
calm Rich tables:

* **Workers**      — running / stale (the 5-minute
  :data:`~polyecho.shared.health.HEARTBEAT_STALE_SECONDS` rule),
  last-seen, phase, datasource, counters.
* **Integrations** — ok / down, detail, latency, checked-at.

This command is strictly **read-only**: it only *reads* heartbeat /
integration_status rows via the shared aggregator.  It never writes, and
— unlike the trader live path — it never touches or trips the
live-trading lock.  It is observability-only and always exits 0 on a
good snapshot (it is a surface, never a gate); it exits 1 *only* if the
aggregator itself raises, and then with a clean one-line message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

_console = Console()

# Calm, non-alarming status glyphs (consistent with `doctor`).
_OK = "[green]●[/green]"
_STALE = "[yellow]●[/yellow]"
_DOWN = "[red]●[/red]"
_UNKNOWN = "[dim]●[/dim]"


# ---------------------------------------------------------------------------
# Duck-typed snapshot access
# ---------------------------------------------------------------------------
#
# The shared StatusSnapshot is a frozen typed (pydantic) object; we read it
# attr-or-key so the CLI stays decoupled from the exact model shape and also
# tolerates a plain mapping (e.g. an already-``model_dump``-ed snapshot).


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first present attribute/key in *names* from *obj*."""
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return default


def _named_items(obj: Any) -> list[tuple[str, Any]]:
    """Coerce a snapshot collection to ``[(name, entry), ...]``.

    The frozen contract delivers ``workers`` (and ``integration.probes``)
    as a ``dict`` *keyed by the logical name* with the name **not**
    repeated inside each value.  We therefore preserve the key as the
    name.  A list/tuple is also accepted (name then taken from the entry
    itself) so the renderer stays robust to either shape.
    """
    if obj is None:
        return []
    if isinstance(obj, dict):
        return [(str(k), v) for k, v in obj.items()]
    if isinstance(obj, (list, tuple)):
        out: list[tuple[str, Any]] = []
        for entry in obj:
            name = _get(entry, "name", "worker", "integration", "id", default="-")
            out.append((str(name), entry))
        return out
    try:
        return [(str(k), v) for k, v in dict(obj).items()]
    except (TypeError, ValueError):
        return []


def _fmt(value: Any) -> str:
    """Render a scalar/timestamp field calmly; ``-`` for empties."""
    if value is None or value == "":
        return "-"
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return str(iso())
    return str(value)


def _latency(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.0f} ms"
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _worker_state(name: str, worker: Any) -> tuple[str, str]:
    """Return (glyph, label) for one worker heartbeat.

    The frozen ``StatusSnapshot.workers`` maps a name to a
    ``WorkerHeartbeat | None``.  ``None`` (row absent / unreadable ``ts``)
    is **down**.  Otherwise the 5-minute staleness verdict is the
    heartbeat's own :meth:`~polyecho.shared.health.WorkerHeartbeat.is_stale`
    — status only *renders* it.  A duck-typed boolean ``running`` or a
    string ``state`` is honoured too so plain mappings still render.
    """
    if worker is None:
        return _DOWN, "down"

    is_stale = getattr(worker, "is_stale", None)
    if callable(is_stale):
        try:
            return (_STALE, "stale") if is_stale() else (_OK, "running")
        except Exception:  # noqa: BLE001 - never let rendering raise
            return _UNKNOWN, "unknown"

    running = _get(worker, "running", "is_running", "alive", "is_alive")
    if running is True:
        return _OK, "running"
    if running is False:
        state = _get(worker, "state", "status")
        if state is not None and "stale" in str(state).lower():
            return _STALE, "stale"
        return _DOWN, "down"

    state = _get(worker, "state", "status")
    if state is not None:
        s = str(state).lower()
        if "run" in s or s == "ok":
            return _OK, str(state)
        if "stale" in s:
            return _STALE, str(state)
        if "down" in s or "dead" in s:
            return _DOWN, str(state)
        return _UNKNOWN, str(state)
    return _UNKNOWN, "unknown"


def _render_workers(workers: list[tuple[str, Any]]) -> Table:
    table = Table(
        title="Workers",
        title_style="bold",
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
    )
    table.add_column("Worker", style="bold", min_width=14)
    table.add_column("State", min_width=10)
    table.add_column("Last seen")
    table.add_column("Phase")
    table.add_column("Datasource")
    table.add_column("Counters")

    if not workers:
        table.add_row("[dim]none[/dim]", "-", "-", "-", "-", "-")
        return table

    for name, w in workers:
        glyph, label = _worker_state(name, w)
        last_seen = _get(
            w, "ts", "last_seen", "last_seen_at", "heartbeat_at", "seen_at"
        )
        phase = _get(w, "phase", "step", "stage")
        datasource = _get(w, "datasource", "data_source", "source")
        counters = _get(w, "counters", "metrics", "stats")

        if isinstance(counters, dict):
            counters_txt = (
                "  ".join(f"{k}={v}" for k, v in counters.items())
                if counters
                else "-"
            )
        else:
            counters_txt = _fmt(counters)

        table.add_row(
            str(name),
            f"{glyph} {label}",
            _fmt(last_seen),
            _fmt(phase),
            _fmt(datasource),
            counters_txt,
        )
    return table


def _render_integrations(integrations: list[tuple[str, Any]]) -> Table:
    table = Table(
        title="Integrations",
        title_style="bold",
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
    )
    table.add_column("Integration", style="bold", min_width=18)
    table.add_column("State", min_width=8)
    table.add_column("Detail")
    table.add_column("Latency", justify="right")
    table.add_column("Checked at")

    if not integrations:
        table.add_row("[dim]none[/dim]", "-", "-", "-", "-")
        return table

    for name, it in integrations:
        ok = _get(it, "ok", "healthy", "up")
        detail = _get(it, "detail", "message", default="")
        latency = _get(it, "latency_ms", "latency")
        checked_at = _get(it, "checked_at", "checked", "at", "last_checked_at")

        if ok is True:
            state = f"{_OK} ok"
        elif ok is False:
            state = f"{_DOWN} down"
        else:
            state = f"{_UNKNOWN} unknown"

        table.add_row(
            str(name),
            state,
            _fmt(detail),
            _latency(latency),
            _fmt(checked_at),
        )
    return table


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------


def status() -> None:
    """Show the aggregated system status (workers + integrations).

    Read-only mirror of the web ``/status`` page.  Reads heartbeat and
    ``integration_status`` rows via the shared aggregator; never writes
    and never touches the live-trading lock.  Always exits 0 (this is an
    observability surface, not a gate).
    """
    from polyecho.shared.health import get_status

    _console.print()
    _console.print("[bold]PolyEcho — System Status[/bold]")
    _console.print()

    try:
        # ``get_status`` is the frozen *async* aggregator — run it to
        # completion synchronously (same pattern as ``doctor``'s DB check).
        snapshot = asyncio.run(get_status())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Could not read system status:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    workers = _named_items(_get(snapshot, "workers", "heartbeats", default={}))

    # Frozen shape: ``StatusSnapshot.integration`` (singular) →
    # ``IntegrationSnapshot`` with ``.probes`` (name-keyed) + ``.checked_at``.
    # Fall back to a flat ``integrations`` collection for robustness.
    integration = _get(snapshot, "integration", "integration_status")
    probes = _get(integration, "probes", default=None)
    if probes is not None:
        integrations = _named_items(probes)
    else:
        integrations = _named_items(
            _get(snapshot, "integrations", default=[])
        )
    sweep_checked_at = _get(integration, "checked_at")

    _console.print(_render_workers(workers))
    _console.print()
    _console.print(_render_integrations(integrations))
    if sweep_checked_at is not None:
        _console.print()
        _console.print(
            f"[dim]Integrations last probed {_fmt(sweep_checked_at)}[/dim]"
        )
    _console.print()

    generated_at = _get(snapshot, "generated_at", "as_of", "timestamp")
    if generated_at is not None:
        _console.print(f"[dim]Snapshot as of {_fmt(generated_at)}[/dim]")

    raise typer.Exit(code=0)
