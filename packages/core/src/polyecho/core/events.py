"""
polyecho.core.events
====================
Best-effort event-emission helper for trader-core (Phase-5 Wave-2).

The trading loop emits PRD §5.8 push events at natural points (copy fill,
position closed, stop-loss / target hit, risk breakers, system health).
:func:`safe_emit` wraps the FROZEN Wave-1 :func:`polyecho.shared.events.emit_event`
so that **a NOTIFY / DB hiccup can never crash or stall the trading loop**:
every failure is logged and swallowed (the money path keeps running).

This module is import-side-effect-free; it only re-exports the frozen
:class:`EventType` / :class:`Severity` / :class:`PushPayload` symbols and
adds the defensive wrapper.
"""

from __future__ import annotations

import logging
from typing import Any

from polyecho.shared.events import (
    EventType,
    PushPayload,
    Severity,
    emit_event,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EventType",
    "PushPayload",
    "Severity",
    "safe_emit",
]


async def safe_emit(
    *,
    event_type: EventType,
    severity: Severity = Severity.INFO,
    payload: dict[str, Any],
    push: PushPayload | None = None,
    session: Any = None,
) -> int | None:
    """Emit one event, swallowing **any** failure (best-effort).

    Delegates to the frozen :func:`polyecho.shared.events.emit_event`.  A
    NOTIFY / DB / connection error here MUST NOT propagate into the
    copy-trade loop, so every exception is caught, logged and converted to
    a ``None`` return — the loop continues exactly as if the event had
    been sent.

    Args:
        event_type: One of :class:`EventType`.
        severity:   One of :class:`Severity` (default :attr:`Severity.INFO`).
        payload:    Caller-defined event data.
        push:       Optional :class:`PushPayload` for the Service Worker.
        session:    Optional caller :class:`AsyncSession`.

    Returns:
        The new ``events.id`` on success, or ``None`` if emission failed
        (logged, never raised).
    """
    try:
        return await emit_event(
            event_type=event_type,
            severity=severity,
            payload=payload,
            push=push,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: never stall the loop
        logger.warning(
            "event_emit_failed_continuing",
            extra={
                "event_type": str(event_type),
                "severity": str(severity),
                "error": str(exc),
            },
        )
        return None
