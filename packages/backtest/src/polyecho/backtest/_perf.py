"""
polyecho.backtest._perf
=======================
Minimal, **best-effort** job-duration timing for the backtest engine
(PRD §7.1 — a single 90-day backtest should complete in < 60 s).

Observability only: NO new behaviour, NO control flow, NO event types
(the shared ``EventType`` enum is frozen — this is a LOG field, not an
event).  A clock failure can never propagate into the job outcome.

Stdlib ``time.perf_counter`` only (monotonic).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

#: PRD §7.1 single 90d backtest target (informational — NOT enforced).
BACKTEST_TARGET_SECONDS: float = 60.0


@contextmanager
def perf_timer() -> Iterator[list[float]]:
    """Monotonic elapsed-seconds timer (best-effort, never raises).

    Yields a single-element list whose ``[0]`` slot holds the elapsed
    seconds once the ``with`` block exits, or stays at ``-1.0`` if the
    clock misbehaves.  The wrapped job is unaffected either way.
    """
    holder: list[float] = [-1.0]
    try:
        start = time.perf_counter()
    except Exception:  # noqa: BLE001 - clock failure must not break the job
        start = None
    try:
        yield holder
    finally:
        if start is not None:
            try:
                holder[0] = max(0.0, time.perf_counter() - start)
            except Exception:  # noqa: BLE001 - timing is strictly best-effort
                holder[0] = -1.0
