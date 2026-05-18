"""
polyecho.shared.events
=======================
**FROZEN Phase-5 Wave-1 contract module.**

Wave-2 (core / cli / push-sender / web) imports the enums, the
:class:`PushPayload` dataclass and :func:`emit_event` *verbatim*.  Every
public symbol here is a binding API surface — additive evolution only.

What lives here (and only here)
-------------------------------
* :class:`EventType`   — the PRD §5.8 push-event taxonomy (the
                         ``events.event_type`` value set).
* :class:`Severity`    — ``events.severity`` value set (default ``info``).
* :class:`PushPayload` — exactly what the Service Worker renders.
* :data:`EVENTS_NOTIFY_CHANNEL` — the Postgres ``LISTEN/NOTIFY`` channel
                         ``web-push-sender`` subscribes to (PRD §4.1).
* :func:`emit_event`   — append one ``events`` row + wake the sender.

NOTIFY ordering & session ownership (R2)
----------------------------------------
``emit_event`` writes the row, ensures it is **committed**, then issues
``NOTIFY polyecho_events, '<event_id>'``.  The NOTIFY is sent strictly
*after* the commit so that when ``web-push-sender`` receives the id it can
always ``SELECT`` the row (a NOTIFY fired inside an uncommitted txn would
race the listener).  The payload is the integer event id rendered as a
decimal string — safe against SQL injection by construction (it is an
``int``), so a parameter-bound ``pg_notify(channel, payload)`` call is
used.

**Session ownership (R2 fix).**  ``emit_event`` only commits a session it
*created itself* (the ``session is None`` path).  When the caller supplies
a session, ``emit_event`` does **not** commit it — committing a caller's
in-flight transaction under them silently ends their unit of work.  For a
caller-supplied session the row is ``flush``-ed (so the id is assigned and
visible within the caller's txn) and the NOTIFY is deferred: it is the
caller's responsibility to commit; the wake-up for that case is recovered
by the sender's durable backfill on its next real notification.  The owned
path commits, fires the NOTIFY, and commits again.  If the
``pg_notify`` send raises, the row is already durably committed but the
wake-up is lost; this is recoverable (the sender re-reconciles from its
cursor) **but must be visible**, so a WARNING is logged rather than
swallowed silently.

No network / SDK I/O on import.  Pure types + the emit helper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import bindparam, text

from polyecho.shared.db import session_scope
from polyecho.shared.models import Event

try:  # typing-only import; never required at runtime
    from sqlalchemy.ext.asyncio import AsyncSession
except Exception:  # pragma: no cover - sqlalchemy always present at runtime
    AsyncSession = Any  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

__all__ = [
    "EVENTS_NOTIFY_CHANNEL",
    "EventType",
    "PushPayload",
    "Severity",
    "emit_event",
]

#: Postgres ``LISTEN/NOTIFY`` channel ``web-push-sender`` subscribes to so it
#: wakes the instant a new ``events`` row is committed (PRD §4.1 instant sync,
#: no polling).  Frozen — Wave-2's sender LISTENs on exactly this string.
EVENTS_NOTIFY_CHANNEL: Final = "polyecho_events"


class EventType(StrEnum):
    """PRD §5.8 推播事件 taxonomy — the ``events.event_type`` value set.

    Wave-2 services emit exactly these; the Service Worker / web inbox key
    notification grouping off these values.  Additive evolution only.
    """

    COPY_FILL = "copy_fill"
    POSITION_CLOSED = "position_closed"
    STOP_LOSS = "stop_loss"
    TARGET_HIT = "target_hit"
    DAILY_PNL_SUMMARY = "daily_pnl_summary"
    RISK_DAILY_LOSS = "risk_daily_loss"
    RISK_STRATEGY_DRIFT = "risk_strategy_drift"
    SYSTEM_API_FAILURE = "system_api_failure"
    SYSTEM_BOT_RESTART = "system_bot_restart"
    SYSTEM_LOW_BALANCE = "system_low_balance"
    CANDIDATE_HIGH_SCORE = "candidate_high_score"
    PENDING_DANGEROUS_ACTION = "pending_dangerous_action"


class Severity(StrEnum):
    """``events.severity`` value set — defaults to :attr:`INFO`.

    Mirrors the DB column default ``'info'``; ``CRITICAL`` events MAY be
    surfaced with a louder Service-Worker presentation by Wave-2.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class PushPayload:
    """The exact notification shape the browser Service Worker renders.

    :attr:`url` is an in-app deep-link *path* the SW opens on click
    (defaults to ``"/"``).  :attr:`tag` is the dedupe/collapse key
    (browsers replace a prior notification sharing the same tag).
    :attr:`data` is opaque JSON forwarded to the SW ``notificationclick``
    handler.

    ``to_json`` is the FROZEN serialization Wave-2's sender ships to the
    browser — its key set is part of the contract.
    """

    title: str
    body: str
    url: str = "/"
    tag: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Render exactly what the Service Worker consumes.

        Returns a plain ``dict`` with the frozen keys
        ``{title, body, url, tag, data}`` — stored verbatim in the
        ``events.payload`` jsonb under the ``"push"`` key and delivered
        unchanged to the browser.
        """
        return {
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "tag": self.tag,
            "data": dict(self.data),
        }


async def emit_event(
    *,
    event_type: EventType,
    severity: Severity = Severity.INFO,
    payload: dict[str, Any],
    push: PushPayload | None = None,
    session: AsyncSession | None = None,
) -> int:
    """Append one ``events`` row and wake ``web-push-sender`` via NOTIFY.

    The jsonb ``events.payload`` column stores
    ``{"data": <payload>, "push": <push.to_json() or None>}``.
    ``pushed_at`` is left ``NULL`` — the sender stamps it after delivery.

    Session ownership (R2): when *session* is ``None`` this function owns
    the session — it commits, fires ``NOTIFY polyecho_events, '<id>'``
    **after the commit**, and commits again.  When the caller supplies
    *session*, the row is flushed (id assigned, visible in the caller's
    txn) but **not committed** (committing the caller's in-flight txn under
    them is the R2 bug) and the NOTIFY is **not** sent here — the caller
    commits and the sender's durable backfill covers the wake-up.  If the
    owned-path ``pg_notify`` send fails the row is still durably committed;
    the lost wake-up is recoverable but is logged at WARNING so it is
    visible.  ``pg_notify`` uses a bound parameter; the id is an ``int`` so
    the channel payload is injection-safe by construction.

    Args:
        event_type: One of :class:`EventType`.
        severity:   One of :class:`Severity` (default :attr:`Severity.INFO`).
        payload:    Caller-defined event data (stored under ``"data"``).
        push:       Optional :class:`PushPayload` the SW will render.
        session:    Optional caller session; if ``None`` a new
                    :func:`~polyecho.shared.db.session_scope` is opened
                    (only then does ``emit_event`` commit / NOTIFY).

    Returns:
        The new ``events.id`` (``int``).
    """
    body = {
        "data": payload,
        "push": push.to_json() if push is not None else None,
    }

    def _log_emitted(event_id: int, *, notified: bool) -> None:
        logger.info(
            "event_emitted",
            extra={
                "event_id": event_id,
                "event_type": str(event_type),
                "severity": str(severity),
                "has_push": push is not None,
                "notified": notified,
            },
        )

    async def _emit_owned(sess: AsyncSession) -> int:
        """``session is None`` path — we own the session: commit + NOTIFY."""
        row = Event(
            event_type=str(event_type),
            severity=str(severity),
            payload=body,
        )
        sess.add(row)
        # Flush so the Identity PK is assigned before we build the NOTIFY.
        await sess.flush()
        event_id = int(row.id)
        # Commit first: the listener must be able to SELECT the row the
        # instant it receives the id (a NOTIFY inside an open txn would
        # race the sender).
        await sess.commit()
        try:
            await sess.execute(
                text("SELECT pg_notify(:chan, :payload)").bindparams(
                    bindparam("chan", EVENTS_NOTIFY_CHANNEL),
                    bindparam("payload", str(event_id)),
                )
            )
            await sess.commit()
        except Exception as exc:  # noqa: BLE001 - wake-up loss must be visible
            # The row is durably committed; only the advisory wake-up was
            # lost.  The sender re-reconciles from its cursor, so this is
            # recoverable — but it MUST be visible, not swallowed silently.
            logger.warning(
                "event_notify_failed_wakeup_lost",
                extra={
                    "event_id": event_id,
                    "channel": EVENTS_NOTIFY_CHANNEL,
                    "error": str(exc),
                },
            )
            _log_emitted(event_id, notified=False)
            return event_id
        _log_emitted(event_id, notified=True)
        return event_id

    async def _emit_borrowed(sess: AsyncSession) -> int:
        """Caller-supplied session — DO NOT commit it / DO NOT NOTIFY here.

        Committing a caller's in-flight transaction under them (the R2 bug)
        silently ends their unit of work.  We only flush so the id is
        assigned and visible within their txn; the caller commits, and the
        sender's durable backfill recovers the (here-undelivered) wake-up.
        """
        row = Event(
            event_type=str(event_type),
            severity=str(severity),
            payload=body,
        )
        sess.add(row)
        await sess.flush()
        event_id = int(row.id)
        _log_emitted(event_id, notified=False)
        return event_id

    if session is not None:
        return await _emit_borrowed(session)
    async with session_scope() as owned:
        return await _emit_owned(owned)
