"""
polyecho.core._perf
====================
Minimal, **best-effort** performance timing for the copy-trade hot path
(PRD §7.1 copy-trade latency < 5 s; PRD §2.3 on-chain timestamp lag).

This is *observability only* — it adds NO new behaviour, NO control flow,
NO event types (the shared ``EventType`` enum is frozen).  Every timing
helper here is wrapped so a clock / arithmetic failure can **never**
propagate into the trade path: a broken timer logs nothing and the trade
proceeds exactly as before.

Stdlib only (``time.perf_counter`` for monotonic elapsed; the wall clock
is only used for the source-timestamp lag, which is inherently a
wall-clock delta).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

#: PRD §7.1 copy-trade latency target (informational log field so the
#: structured log line is self-describing — NOT an enforced threshold).
COPY_LATENCY_TARGET_MS: float = 5_000.0


@contextmanager
def perf_timer() -> Iterator[list[float]]:
    """Monotonic elapsed-ms timer (best-effort, never raises).

    Yields a single-element list whose ``[0]`` slot holds the elapsed
    milliseconds once the ``with`` block exits.  If the monotonic clock
    misbehaves the slot stays at its ``-1.0`` sentinel and callers simply
    skip logging — the wrapped work is unaffected either way.
    """
    holder: list[float] = [-1.0]
    try:
        start = time.perf_counter()
    except Exception:  # noqa: BLE001 - clock failure must not break the path
        start = None
    try:
        yield holder
    finally:
        if start is not None:
            try:
                holder[0] = max(0.0, (time.perf_counter() - start) * 1000.0)
            except Exception:  # noqa: BLE001 - timing is strictly best-effort
                holder[0] = -1.0


def detect_lag_ms(source_ts_epoch: float | int | None) -> float | None:
    """Wall-clock lag (ms) between *now* and a leader trade's source ts.

    ``source_ts_epoch`` is the on-chain / datasource Unix timestamp
    (seconds) carried on the detected-trade dict (PRD §2.3 "比對鏈上時間
    戳").  Returns ``None`` (caller omits the field) on any bad/missing
    value — this is purely informational and must never raise.
    """
    if source_ts_epoch is None:
        return None
    try:
        lag = (time.time() - float(source_ts_epoch)) * 1000.0
    except Exception:  # noqa: BLE001 - never break the trade path
        return None
    # A negative lag means clock skew between us and the source; clamp to
    # 0 so the field stays a sane non-negative latency measurement.
    return max(0.0, lag)
