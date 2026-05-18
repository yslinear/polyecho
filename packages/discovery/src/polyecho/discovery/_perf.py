"""
polyecho.discovery._perf
========================
Minimal, **best-effort** cycle-duration timing for the discovery
pipelines (PRD §7.1 — a full recompute should complete in < 30 min).

Observability only: NO new behaviour, NO control flow, NO event types
(the shared ``EventType`` enum is frozen — this is a LOG field, not an
event).  A clock failure can never propagate into the discovery pass.

Stdlib ``time.perf_counter`` only (monotonic).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

#: PRD §7.1 full-recompute target (informational log field — NOT enforced).
DISCOVERY_CYCLE_TARGET_SECONDS: float = 1_800.0


@contextmanager
def perf_timer() -> Iterator[list[float]]:
    """Monotonic elapsed-seconds timer (best-effort, never raises).

    Yields a single-element list whose ``[0]`` slot holds the elapsed
    seconds once the ``with`` block exits, or stays at ``-1.0`` if the
    clock misbehaves.  The wrapped discovery work is unaffected either
    way.
    """
    holder: list[float] = [-1.0]
    try:
        start = time.perf_counter()
    except Exception:  # noqa: BLE001 - clock failure must not break the pass
        start = None
    try:
        yield holder
    finally:
        if start is not None:
            try:
                holder[0] = max(0.0, time.perf_counter() - start)
            except Exception:  # noqa: BLE001 - timing is strictly best-effort
                holder[0] = -1.0
