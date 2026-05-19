"""
polyecho.backtest._heartbeat
============================
Strictly **fail-soft** ``backtest_heartbeat`` liveness emission.

The backtest worker writes one heartbeat per poll cycle (and on the
``--once`` drain) so ``polyecho`` status surfaces / the Dashboard
(PRD §7.4/§2.3) can show the backtest worker as UP/DOWN and when it
last polled.

FROZEN contract (F0 — owned by ``polyecho.shared.health``, OUT of this
package; this module only *calls* it):

    polyecho.shared.health.write_heartbeat(
        "backtest", phase=..., datasource=..., live_trading=..., counters=...
    )   # keyword-only after ``kind``; F0 stamps ``ts`` itself

writes JSON to the ``backtest_heartbeat`` ``settings`` key with shape
``{ts ISO8601 REQUIRED, phase, datasource, live_trading, counters{}}``.

HARD invariant: a heartbeat write (or even resolving its inputs, or
importing the frozen shared module) MUST NEVER abort the backtest
worker or perturb job processing.  Everything here is wrapped so any
exception — ``ImportError`` included (the shared module may legitimately
be absent in an isolated build) — degrades to a swallowed WARNING and
the worker keeps draining jobs.  Mirrors the established best-effort
pattern in ``polyecho.core`` (``Trader._write_heartbeat`` /
``wiring.make_heartbeat_writer``).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Heartbeat ``phase`` values (mirrors the worker's poll state machine).
PHASE_IDLE = "idle"
PHASE_RUNNING = "running"


def _resolve_datasource() -> str:
    """Best-effort current datasource name ("fixture"/"live").

    Wrapped: a config/parse failure must not break the heartbeat (the
    caller is already inside a fail-soft guard, but keeping this total
    means a partial heartbeat with a sane default still goes out).
    """
    try:
        from polyecho.shared.config import get_settings

        return str(get_settings().datasource)
    except Exception:  # noqa: BLE001 - liveness is strictly best-effort
        return "unknown"


def _resolve_live_trading() -> bool:
    """Best-effort master live-trading flag (defaults to the safe False)."""
    try:
        from polyecho.shared.config import get_settings

        return bool(get_settings().live_trading)
    except Exception:  # noqa: BLE001 - liveness is strictly best-effort
        return False


async def write_backtest_heartbeat(
    phase: str,
    counters: dict[str, int],
) -> None:
    """Emit one ``backtest_heartbeat`` (best-effort; never raises).

    Args:
        phase:    ``"running"`` (this cycle claimed/processed jobs) or
                  ``"idle"`` (poll found nothing pending).
        counters: per-cycle tallies, e.g.
                  ``{"processed": int, "failed": int}``.

    Resolves the timestamp/datasource/live flag and delegates to the
    FROZEN ``polyecho.shared.health.write_heartbeat``.  ANY failure —
    importing the (possibly-absent) shared module, resolving inputs, or
    the write itself — is logged at WARNING and swallowed so the
    backtest worker continues draining jobs unaffected.
    """
    try:
        datasource = _resolve_datasource()
        live_trading = _resolve_live_trading()

        # Lazy import INSIDE the guard: the frozen shared.health module is
        # owned by another package and may be absent in an isolated build —
        # an ImportError here must degrade to a swallowed warning, never
        # abort the worker.
        from polyecho.shared.health import write_heartbeat

        # Defensive copy so a later mutation of the caller's per-cycle
        # counter dict can never retroactively change what was emitted.
        safe_counters: dict[str, Any] = dict(counters)

        # F0's write_heartbeat is keyword-only after ``kind`` and stamps
        # ``ts`` itself (ISO-8601 UTC) — callers MUST NOT pass ``ts``.
        await write_heartbeat(
            "backtest",
            phase=phase,
            datasource=datasource,
            live_trading=live_trading,
            counters=safe_counters,
        )
    except Exception as exc:  # noqa: BLE001 - heartbeat must never abort work
        logger.warning(
            "backtest_heartbeat_write_failed_continuing",
            extra={"phase": phase, "error": str(exc)},
        )
