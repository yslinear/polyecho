"""Web-push-sender: consumes the ``events`` table and delivers VAPID Web Push.

PRD reference: §5.8 (Web Push alert centre) + §4.1 (instant LISTEN/NOTIFY sync).

Design (Phase-5 Wave-2 — consumes the FROZEN ``polyecho.shared`` Wave-1
contract verbatim):

* **Startup backfill** — events accumulate while every browser is offline
  (PRD §5.8 "離線累積").  On boot we ``SELECT`` every ``events`` row with
  ``pushed_at IS NULL`` ordered by ``id ASC`` and deliver each before we
  begin streaming live notifications, so nothing is lost across restarts.
* **Live stream** — ``async for n in pg_listen_iter("polyecho_events")``;
  each ``n.payload`` is the integer event id (emitted by
  :func:`polyecho.shared.events.emit_event`); we load that row and deliver.
* **Delivery** — ``event.payload["push"]`` is the frozen
  :meth:`PushPayload.to_json` dict (or ``None`` for a non-push event).  For
  a push event we fan out to *every* row in ``push_subscriptions`` via
  ``pywebpush.webpush`` (synchronous → run in a worker thread).  ``404`` /
  ``410`` prunes the dead subscription; ``2xx`` stamps ``last_used_at``;
  transient ``5xx`` / network errors get a bounded retry and, if still
  failing, the event is left ``pushed_at IS NULL`` so the next backfill
  retries it.  Once every subscription has been handled the event's
  ``pushed_at`` is stamped.
* **Daily PnL summary** — an APScheduler cron job at 23:00 ``settings.tz``
  computes a realised-PnL / fills digest from ``my_trades`` and calls
  :func:`emit_event` with ``DAILY_PNL_SUMMARY``; the same loop then delivers
  it like any other event.

This process holds no VAPID *public* key and opens no network ports: it
only reads ``settings.vapid_private_key`` to sign outbound pushes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from pywebpush import WebPushException, webpush
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from polyecho.shared.config import get_settings
from polyecho.shared.db import pg_listen_iter, session_scope
from polyecho.shared.events import (
    EVENTS_NOTIFY_CHANNEL,
    EventType,
    PushPayload,
    Severity,
    emit_event,
)
from polyecho.shared.models import Event, MyTrade, PushSubscription
from polyecho.shared.vapid import build_vapid_claims

logger = logging.getLogger(__name__)
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Event type constants (PRD §5.8) — retained for back-compat with the
# documented Phase-0 contract / smoke tests.  The authoritative taxonomy is
# the frozen :class:`polyecho.shared.events.EventType` StrEnum.
# ---------------------------------------------------------------------------

EVENT_NEW_TRADE = "new_trade"
EVENT_POSITION_CLOSED = "position_closed"
EVENT_DAILY_PNL_SUMMARY = "daily_pnl_summary"
EVENT_RISK_TRIGGERED = "risk_triggered"
EVENT_SYSTEM_ERROR = "system_error"
EVENT_NEW_LEADER_CANDIDATE = "new_leader_candidate"
EVENT_PENDING_ACTION = "pending_action"

ALL_EVENT_TYPES: frozenset[str] = frozenset(
    [
        EVENT_NEW_TRADE,
        EVENT_POSITION_CLOSED,
        EVENT_DAILY_PNL_SUMMARY,
        EVENT_RISK_TRIGGERED,
        EVENT_SYSTEM_ERROR,
        EVENT_NEW_LEADER_CANDIDATE,
        EVENT_PENDING_ACTION,
    ]
)

#: HTTP statuses meaning "this subscription is permanently gone" (RFC 8030 §7
#: / Web Push): the endpoint 404s or the push service reports 410 Gone.  Such
#: rows are deleted so we never retry a dead device.
_GONE_STATUSES: frozenset[int] = frozenset({404, 410})

#: Default Web Push TTL (seconds) — how long the push service retains the
#: message for an offline device before discarding it.  4 weeks matches the
#: PRD §5.8 "離線累積" intent for transient delivery.
_DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 28

#: Bounded retry budget for transient (5xx / network) push failures within a
#: single delivery attempt.  Exhausting it leaves ``pushed_at`` NULL so the
#: next startup backfill retries the whole event.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_BASE_SECONDS = 0.5


def _now() -> datetime:
    """Timezone-aware UTC ``now`` (single seam so tests can monkeypatch)."""
    return datetime.now(UTC)


def _is_db_down(exc: BaseException) -> bool:
    """True if *exc* (or its cause chain) is a DB-connectivity failure.

    SQLAlchemy wraps asyncpg connect errors in ``OperationalError`` /
    ``InterfaceError``; the underlying cause is an ``OSError`` /
    ``ConnectionError``.  Used so ``--once`` (CI smoke) exits 0 when no
    Postgres is reachable while the supervised loop still retries.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError | ConnectionError):
            return True
        mod = type(cur).__module__
        name = type(cur).__name__
        if mod.startswith(("asyncpg", "sqlalchemy")) and name in {
            "OperationalError",
            "InterfaceError",
            "CannotConnectNowError",
            "ConnectionDoesNotExistError",
        }:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class WebPushSender:
    """Consumes the ``events`` table and fans out Web Push notifications.

    PRD §5.8.  See the module docstring for the full delivery contract.  The
    engine is *resilient*: one bad subscription or one malformed event never
    kills the loop — failures are logged (secrets redacted) and the next
    event / next startup backfill proceeds.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Backfill, then stream live events forever (supervised loop).

        Order matters (PRD §5.8): the startup backfill drains everything
        accumulated while browsers were offline *before* we start consuming
        live NOTIFYs, so a restart never drops an event.  The live loop
        reconnects automatically (``pg_listen_iter`` owns a dedicated
        asyncpg connection); any per-event error is contained.
        """
        log.info("web_push_sender_starting", channel=EVENTS_NOTIFY_CHANNEL)
        await self.run_backfill()
        await self._stream_forever()

    async def run_once(self) -> int:
        """Drain every currently-unpushed event then return (CI smoke path).

        This is the ``python -m polyecho.push --once`` entry: it verifies the
        service boots and the drain path is wired.  If the database is
        unreachable (no Postgres in the CI-smoke container) it logs and
        returns ``0`` rather than crashing — the supervised loop is the
        component that *retries* a down DB; ``--once`` must exit cleanly so
        CI can assert "the service starts and drains".

        Returns:
            The number of events processed (``0`` if the DB is unreachable).
        """
        log.info("web_push_sender_once")
        try:
            return await self.run_backfill()
        except OSError as exc:
            # asyncpg surfaces connect failures as OSError /
            # ConnectionError; SQLAlchemy wraps them too — see _is_db_down.
            log.warning("web_push_once_db_unavailable", error=str(exc))
            return 0
        except Exception as exc:  # noqa: BLE001 - smoke must exit 0
            if _is_db_down(exc):
                log.warning("web_push_once_db_unavailable", error=str(exc))
                return 0
            raise

    async def run_backfill(self) -> int:
        """Deliver every ``events`` row with ``pushed_at IS NULL`` (id ASC).

        Returns:
            The number of events processed.
        """
        async with session_scope() as session:
            pending = await self._fetch_pending_event_ids(session)
        log.info("web_push_backfill", pending=len(pending))
        for event_id in pending:
            await self._safe_deliver(event_id)
        return len(pending)

    async def push_event(self, event: dict[str, Any]) -> None:
        """Deliver a single already-loaded event row dict to all subs.

        Kept for the documented Phase-0 contract.  ``event`` must contain at
        least ``id`` and ``payload`` (the frozen
        ``{"data": ..., "push": ...}`` jsonb shape).
        """
        async with session_scope() as session:
            row = await session.get(Event, int(event["id"]))
            if row is None:
                return
            await self._deliver_event(session, row)

    # ------------------------------------------------------------------
    # Live stream
    # ------------------------------------------------------------------

    async def _stream_forever(self) -> None:
        """Consume the NOTIFY stream; reconnect on any connection drop."""
        while True:
            try:
                async with pg_listen_iter(EVENTS_NOTIFY_CHANNEL) as notes:
                    log.info("web_push_listening", channel=EVENTS_NOTIFY_CHANNEL)
                    async for note in notes:
                        await self._handle_notification(note.payload)
            except asyncio.CancelledError:
                log.info("web_push_sender_cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - resilient reconnect loop
                log.warning("web_push_stream_lost", error=str(exc))
                await asyncio.sleep(_RETRY_BACKOFF_BASE_SECONDS)

    async def _handle_notification(self, payload: str) -> None:
        """Parse a NOTIFY payload (an int event id) and deliver that event."""
        try:
            event_id = int(payload)
        except (TypeError, ValueError):
            log.warning("web_push_bad_notify_payload", payload=payload)
            return
        await self._safe_deliver(event_id)

    async def _safe_deliver(self, event_id: int) -> None:
        """Deliver one event by id; never let one failure kill the loop."""
        try:
            async with session_scope() as session:
                row = await session.get(Event, event_id)
                if row is None:
                    log.warning("web_push_event_missing", event_id=event_id)
                    return
                if row.pushed_at is not None:
                    # Already delivered (backfill/live race) — idempotent skip.
                    return
                await self._deliver_event(session, row)
        except Exception as exc:  # noqa: BLE001 - one event never kills loop
            log.error(
                "web_push_event_failed", event_id=event_id, error=str(exc)
            )

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _deliver_event(self, session: AsyncSession, event: Event) -> None:
        """Fan one event out to every subscription, then stamp ``pushed_at``.

        Non-push events (``payload["push"] is None``) are stamped and
        skipped — no ``webpush`` call.  If *any* subscription hits an
        exhausted transient failure the event is left ``pushed_at IS NULL``
        so the next backfill retries it.
        """
        payload = event.payload or {}
        push_json = payload.get("push") if isinstance(payload, dict) else None

        if push_json is None:
            event.pushed_at = _now()
            log.info(
                "web_push_non_push_event",
                event_id=event.id,
                event_type=event.event_type,
            )
            return

        subs = await self._fetch_subscriptions(session)
        if not subs:
            event.pushed_at = _now()
            log.info("web_push_no_subscriptions", event_id=event.id)
            return

        data = json.dumps(push_json, separators=(",", ":"))
        all_durable = True
        for sub in subs:
            outcome = await self._deliver_to_subscription(session, sub, data)
            if outcome == "transient":
                all_durable = False

        if all_durable:
            event.pushed_at = _now()
            log.info(
                "web_push_event_delivered",
                event_id=event.id,
                event_type=event.event_type,
                subscriptions=len(subs),
            )
        else:
            log.warning(
                "web_push_event_deferred",
                event_id=event.id,
                event_type=event.event_type,
            )

    async def _deliver_to_subscription(
        self,
        session: AsyncSession,
        sub: PushSubscription,
        data: str,
    ) -> str:
        """Deliver to one subscription with bounded transient retry.

        Returns one of ``"ok"`` (2xx — ``last_used_at`` stamped),
        ``"gone"`` (404/410 — row deleted), or ``"transient"`` (5xx /
        network, retries exhausted — event must NOT be marked pushed).
        """
        sub_id = sub.id
        for attempt in range(1, _MAX_TRANSIENT_RETRIES + 1):
            try:
                await asyncio.to_thread(self._webpush_blocking, sub, data)
            except WebPushException as exc:
                status = (
                    exc.response.status_code
                    if exc.response is not None
                    else None
                )
                if status in _GONE_STATUSES:
                    await session.execute(
                        delete(PushSubscription).where(
                            PushSubscription.id == sub_id
                        )
                    )
                    log.info(
                        "web_push_subscription_pruned",
                        subscription_id=str(sub_id),
                        status=status,
                    )
                    return "gone"
                if status is not None and 400 <= status < 500:
                    # Non-410/404 4xx (e.g. malformed) — not retryable and
                    # not "gone"; treat as durable failure for this sub so a
                    # single bad row never blocks the event forever.
                    log.error(
                        "web_push_client_error",
                        subscription_id=str(sub_id),
                        status=status,
                    )
                    return "ok"
                log.warning(
                    "web_push_transient_error",
                    subscription_id=str(sub_id),
                    status=status,
                    attempt=attempt,
                )
            except Exception as exc:  # noqa: BLE001 - network/SSL/etc.
                log.warning(
                    "web_push_network_error",
                    subscription_id=str(sub_id),
                    attempt=attempt,
                    error=str(exc),
                )
            else:
                await session.execute(
                    update(PushSubscription)
                    .where(PushSubscription.id == sub_id)
                    .values(last_used_at=_now())
                )
                log.info(
                    "web_push_delivered", subscription_id=str(sub_id)
                )
                return "ok"

            if attempt < _MAX_TRANSIENT_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_BASE_SECONDS * attempt)

        return "transient"

    def _webpush_blocking(self, sub: PushSubscription, data: str) -> None:
        """Synchronous ``pywebpush.webpush`` call (run via ``to_thread``).

        Raises:
            WebPushException: on any non-2xx push-service response.
        """
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {
                    "p256dh": sub.p256dh_key,
                    "auth": sub.auth_key,
                },
            },
            data=data,
            vapid_private_key=self._settings.vapid_private_key.get_secret_value(),
            vapid_claims=build_vapid_claims(),
            ttl=_DEFAULT_TTL_SECONDS,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def _fetch_pending_event_ids(
        self, session: AsyncSession
    ) -> list[int]:
        """Event ids with ``pushed_at IS NULL`` ordered ``id ASC``."""
        result = await session.execute(
            select(Event.id)
            .where(Event.pushed_at.is_(None))
            .order_by(Event.id.asc())
        )
        return [int(row) for row in result.scalars().all()]

    async def _fetch_subscriptions(
        self, session: AsyncSession
    ) -> list[PushSubscription]:
        """All registered push subscription rows."""
        result = await session.execute(select(PushSubscription))
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Daily PnL summary (PRD §5.8 — 23:00 Asia/Taipei nightly digest)
    # ------------------------------------------------------------------

    async def emit_daily_pnl_summary(self) -> int:
        """Compute today's realised PnL / fills digest and emit the event.

        Aggregates ``my_trades`` filled today (``fill_status == 'filled'``):
        realised PnL is ``SUM(pnl_usd)`` over rows with a non-null
        ``pnl_usd``; fills is the row count.  Emits a
        ``DAILY_PNL_SUMMARY`` event with a Chinese-language
        :class:`PushPayload`; the normal delivery loop ships it.

        Returns:
            The new ``events.id``.
        """
        async with session_scope() as session:
            pnl_row = await session.execute(
                select(
                    func.coalesce(func.sum(MyTrade.pnl_usd), 0),
                    func.count(MyTrade.id),
                ).where(MyTrade.fill_status == "filled")
            )
            total_pnl_raw, fills = pnl_row.one()
            total_pnl = Decimal(str(total_pnl_raw or 0))

            sign = "+" if total_pnl >= 0 else ""
            body = f"今日已實現損益 {sign}{total_pnl:.2f} USD，成交 {fills} 筆"
            event_id = await emit_event(
                event_type=EventType.DAILY_PNL_SUMMARY,
                severity=Severity.INFO,
                payload={
                    "realized_pnl_usd": float(total_pnl),
                    "fills": int(fills),
                },
                push=PushPayload(
                    title="每日跟單摘要",
                    body=body,
                    url="/",
                    tag="daily-pnl-summary",
                ),
                session=session,
            )
        log.info(
            "web_push_daily_summary_emitted",
            event_id=event_id,
            realized_pnl_usd=float(total_pnl),
            fills=int(fills),
        )
        return event_id
