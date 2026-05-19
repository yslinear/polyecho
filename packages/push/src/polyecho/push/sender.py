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
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

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

#: HTTP statuses meaning "our VAPID auth is bad" (RFC 8292): 401 Unauthorized
#: / 403 Forbidden from the push service.  This is a *config* fault (wrong /
#: expired ``vapid_private_key`` or claims), not a per-subscription problem —
#: retrying every subscription would just hammer the service with the same
#: bad key.  We raise :class:`PushConfigError` to halt delivery so the
#: operator fixes VAPID rather than silently stamping events "delivered".
_VAPID_AUTH_STATUSES: frozenset[int] = frozenset({401, 403})

#: HTTP 413 Payload Too Large: the encrypted body exceeded the push service's
#: limit (RFC 8030 §7.2).  This is a *content* fault for this event — never a
#: real delivery and not retryable (the same oversized payload will always
#: 413).  We stamp the event so it is not retried forever, but classify the
#: per-subscription outcome distinctly so it never counts as a real delivery.
_CONTENT_TOO_LARGE_STATUS = 413

#: Default Web Push TTL (seconds) — how long the push service retains the
#: message for an offline device before discarding it.  4 weeks matches the
#: PRD §5.8 "離線累積" intent for transient delivery.
_DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 28

#: Bounded retry budget for transient (5xx / network) push failures within a
#: single delivery attempt.  Exhausting it leaves ``pushed_at`` NULL so the
#: next startup backfill retries the whole event.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_BASE_SECONDS = 0.5


#: Default period (seconds) for the safety-net fallback scan (R13 #3 / R2).
#: The live path is LISTEN/NOTIFY, but ``pg_listen_iter``'s bounded buffer
#: *drops the oldest NOTIFY* on overflow (see ``polyecho.shared.db``
#: ``pg_listen_queue_overflow_dropped_oldest``).  A dropped NOTIFY has no
#: reconnect/restart to trigger the existing backfill, so that event would
#: sit ``pushed_at IS NULL`` forever.  An independent periodic re-scan of
#: ``pushed_at IS NULL`` (idempotent with the existing
#: ``pushed_at IS NOT NULL`` skip in :meth:`_safe_deliver`) guarantees a
#: dropped NOTIFY can strand an event for at most one interval.  300s
#: (5 min) bounds worst-case latency without meaningfully loading the DB
#: (one cheap indexed ``SELECT ... WHERE pushed_at IS NULL``).
_FALLBACK_SCAN_DEFAULT_SECONDS = 300

#: Floor so a mis-set env can't turn the safety net into a tight busy-loop.
_FALLBACK_SCAN_MIN_SECONDS = 10


def _fallback_scan_interval_seconds() -> int:
    """Resolve the fallback-scan period (env-overridable, floored).

    Read from ``POLYECHO_PUSH_FALLBACK_SCAN_SECONDS`` (consistent with the
    repo-wide ``POLYECHO_`` env convention) so operators can tune the
    safety-net cadence without a code change.  An unset / unparseable /
    too-small value falls back to the safe default; a non-positive or
    sub-floor value is clamped to :data:`_FALLBACK_SCAN_MIN_SECONDS` so a
    typo can never produce a CPU/DB-hammering tight loop.
    """
    raw = os.environ.get("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS")
    if raw is None or raw.strip() == "":
        return _FALLBACK_SCAN_DEFAULT_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning(
            "web_push_fallback_scan_bad_env",
            value=raw,
            using=_FALLBACK_SCAN_DEFAULT_SECONDS,
        )
        return _FALLBACK_SCAN_DEFAULT_SECONDS
    if value < _FALLBACK_SCAN_MIN_SECONDS:
        log.warning(
            "web_push_fallback_scan_floored",
            requested=value,
            using=_FALLBACK_SCAN_MIN_SECONDS,
        )
        return _FALLBACK_SCAN_MIN_SECONDS
    return value


#: Cap on concurrent ``webpush`` worker threads per event fan-out (I6).
#: Per-subscription delivery runs ``asyncio.to_thread`` (blocking
#: ``pywebpush`` call); without a bound, one event with thousands of
#: subscriptions — or a handful of very slow endpoints — would either spawn
#: an unbounded thread pile-up or, when serial, head-of-line block the whole
#: loop.  20 keeps fan-out parallel while bounding thread + socket pressure.
_FANOUT_CONCURRENCY = 20


class PushConfigError(RuntimeError):
    """Fatal misconfiguration that must halt delivery (I4).

    Raised when the push service rejects our VAPID credentials (401/403):
    every subscription would fail identically, so retrying is pointless and
    silently stamping the event "delivered" would lose it.  The supervised
    loop surfaces this instead of swallowing it, so the operator fixes the
    ``vapid_private_key`` / claims rather than the bug hiding as "ok".
    """


#: The component name this process reports under in the FROZEN
#: ``polyecho.shared.health.write_heartbeat`` contract (F0): the heartbeat is
#: persisted to the ``push_heartbeat`` settings key with a payload of
#: ``{ts (ISO8601, REQUIRED), phase, datasource, live_trading, counters{}}``.
_HEARTBEAT_COMPONENT = "push"

#: Liveness ``phase`` values reported in the heartbeat payload.  ``idle`` =
#: the loop is parked waiting on the next NOTIFY / next scheduler tick (no
#: in-flight delivery); ``sending`` = a (mini-)backfill / fallback scan /
#: ``--once`` drain is actively delivering this cycle.
_PHASE_IDLE = "idle"
_PHASE_SENDING = "sending"


@dataclass
class _HeartbeatCounters:
    """Per-cycle delivery tally surfaced in the heartbeat ``counters`` block.

    A "cycle" is one drain unit — a (mini-)backfill, a fallback scan, or the
    ``--once`` run.  Counts are *cumulative for that cycle* and reset by
    constructing a fresh instance at the top of each cycle so an operator
    reading the heartbeat sees what the most recent cycle actually did:

    * ``delivered`` — events with at least one real 2xx push this cycle;
    * ``content_error`` — events stamped solely via the content-error path
      (every sub 413 / non-gone 4xx — durable but zero real delivery, the
      :data:`web_push_event_content_error_stamped` case);
    * ``fallback_scanned`` — events processed by the periodic safety-net
      :meth:`WebPushSender.run_fallback_scan` this cycle (0 outside it).
    """

    delivered: int = 0
    content_error: int = 0
    fallback_scanned: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "delivered": self.delivered,
            "content_error": self.content_error,
            "fallback_scanned": self.fallback_scanned,
        }


def _now() -> datetime:
    """Timezone-aware UTC ``now`` (single seam so tests can monkeypatch)."""
    return datetime.now(UTC)


def _start_of_today(tz_name: str) -> datetime:
    """UTC instant of local midnight *today* in ``tz_name`` (P1-8).

    The daily-PnL push body says "今日" (today); the aggregate must therefore
    count only trades whose fill time is on or after local midnight in the
    configured display timezone, converted back to a tz-aware UTC bound for
    the ``TIMESTAMPTZ`` comparison.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - bad tz string ⇒ fall back to UTC
        tz = ZoneInfo("UTC")
    local_now = datetime.now(tz)
    local_midnight = local_now.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(UTC)


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
    # Heartbeat (FROZEN polyecho.shared.health contract, F0) — STRICTLY
    # fail-soft: a heartbeat-write error must NEVER abort the push loop, the
    # ``--once`` backfill, or the scheduler.
    # ------------------------------------------------------------------

    async def _emit_heartbeat(
        self, phase: str, counters: _HeartbeatCounters
    ) -> None:
        """Write one ``push_heartbeat`` via the frozen shared contract.

        Calls ``polyecho.shared.health.write_heartbeat("push", phase=,
        datasource=, live_trading=, counters=)`` — keyword-only after
        ``kind``; F0 stamps the ISO8601 UTC ``ts`` itself.  Passes the live
        datasource / live-trading flags from settings and this cycle's tally.

        Fail-soft is the whole point of this seam.  The heartbeat is pure
        observability; it must never be able to take down delivery.  So:

        * the import is **lazy** — ``polyecho.shared.health`` lives outside
          this worktree and may be absent/broken in an isolated build; a
          module-level import would make *every* push path (including the
          ``--once`` CI smoke and the supervised loop) fail at import time;
        * **every** exception is logged and swallowed *except*
          :class:`asyncio.CancelledError`, which must always propagate so a
          shutdown / task-cancel is never silently eaten (swallowing it
          would wedge graceful shutdown and is not a "heartbeat failure").

        ``write_heartbeat`` may be sync or a coroutine depending on the F0
        implementation; we await it only if it returned an awaitable so
        either shape is tolerated without coupling to one.
        """
        try:
            from polyecho.shared.health import write_heartbeat

            result = write_heartbeat(
                _HEARTBEAT_COMPONENT,
                phase=phase,
                datasource=self._settings.datasource,
                live_trading=self._settings.live_trading,
                counters=counters.as_dict(),
            )
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            # Cooperative cancellation is not a heartbeat failure — never
            # swallow it or graceful shutdown wedges.
            raise
        except BaseException as exc:  # noqa: BLE001 - heartbeat is fail-soft
            # Anything else (import error in an isolated build, a DB blip
            # inside write_heartbeat, a contract mismatch, …) is logged and
            # swallowed so the push loop / --once / scheduler is untouched.
            log.warning("web_push_heartbeat_write_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Stream live events forever, LISTEN-before-backfill (P1-7).

        Ordering is the whole correctness story here.  The naive order
        (backfill → then LISTEN) loses every event committed in the window
        between the backfill ``SELECT`` finishing and ``LISTEN`` attaching:
        such events are neither in the backfill snapshot nor in the (not-yet
        attached) NOTIFY stream, so they sit ``pushed_at IS NULL`` until the
        *next* process restart.

        Fix: open the dedicated ``pg_listen`` connection and attach
        ``LISTEN polyecho_events`` *first* (``pg_listen_iter``'s
        ``async with`` enter does exactly this — it ``add_listener``s before
        yielding, and asyncpg's read loop then buffers every inbound NOTIFY
        into the wrapper's bounded queue regardless of whether we are
        iterating yet).  Only *then* run the startup backfill.  Any event
        committed during the backfill therefore lands in the NOTIFY buffer;
        the existing ``pushed_at IS NOT NULL`` idempotency in
        :meth:`_safe_deliver` makes the inevitable backfill/stream overlap a
        no-op (delivered once, not twice).

        On stream loss the same hazard reappears for events committed while
        disconnected, so the reconnect path runs a *mini-backfill*
        (``pushed_at IS NULL``) **before** re-entering the LISTEN loop —
        again under a freshly-attached listener — so a disconnect strands
        nothing until restart.
        """
        log.info("web_push_sender_starting", channel=EVENTS_NOTIFY_CHANNEL)
        await self._stream_forever(initial=True)

    async def run_mini_backfill(
        self, counters: _HeartbeatCounters | None = None
    ) -> int:
        """Re-deliver every ``pushed_at IS NULL`` event (reconnect path).

        Identical query to the startup backfill; named distinctly so the
        reconnect log line and tests can assert it ran *before* the LISTEN
        loop is re-entered, recovering events missed during the disconnect.
        ``counters`` (optional) is forwarded so a reconnect-recovered
        delivery is reflected in that iteration's heartbeat tally.

        Returns:
            The number of events processed.
        """
        return await self.run_backfill(counters=counters)

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
        counters = _HeartbeatCounters()
        try:
            processed = await self.run_backfill(counters=counters)
        except OSError as exc:
            # asyncpg surfaces connect failures as OSError /
            # ConnectionError; SQLAlchemy wraps them too — see _is_db_down.
            log.warning("web_push_once_db_unavailable", error=str(exc))
            # Still emit a heartbeat (fail-soft) so a DB-down --once is
            # observably "the process ran" — never lets a heartbeat error
            # turn this clean exit into a crash.
            await self._emit_heartbeat(_PHASE_IDLE, counters)
            return 0
        except Exception as exc:  # noqa: BLE001 - smoke must exit 0
            if _is_db_down(exc):
                log.warning("web_push_once_db_unavailable", error=str(exc))
                await self._emit_heartbeat(_PHASE_IDLE, counters)
                return 0
            raise
        # Successful drain: report what this single cycle delivered. The
        # heartbeat call is itself fail-soft, so a write error here can
        # never stop ``--once`` from returning its processed count.
        await self._emit_heartbeat(_PHASE_IDLE, counters)
        return processed

    async def run_fallback_scan(self) -> int:
        """Periodic safety-net re-scan of ``pushed_at IS NULL`` (R13 #3 / R2).

        The live delivery path is LISTEN/NOTIFY, but the shared
        ``pg_listen_iter`` buffer is bounded and **drops the oldest NOTIFY**
        on overflow.  A dropped NOTIFY is *not* followed by a
        reconnect/restart, so nothing would ever trigger the existing
        backfill for that event — it would stay ``pushed_at IS NULL``
        forever.  This method is scheduled on an independent timer
        (:func:`_fallback_scan_interval_seconds`) so a dropped NOTIFY can
        strand an event for at most one interval rather than indefinitely.

        It is *exactly* :meth:`run_backfill` (same idempotent query; the
        ``pushed_at IS NOT NULL`` skip in :meth:`_safe_deliver` makes any
        overlap with the live stream a no-op) wrapped in the same
        DB-unavailable tolerance as :meth:`run_once`: the scheduler must
        never crash the supervised process just because the DB blipped
        between scans — the live loop is what *retries* a down DB; the
        safety net simply no-ops until the next tick.

        Returns:
            The number of events processed (``0`` if the DB is unreachable).
        """
        log.info("web_push_fallback_scan")
        counters = _HeartbeatCounters()
        try:
            processed = await self.run_backfill(counters=counters)
        except (asyncio.CancelledError, PushConfigError):
            # Cancellation must propagate; bad VAPID must halt (I4) — never
            # let the safety net swallow either (and a heartbeat write must
            # not run on these paths and risk masking them).
            raise
        except OSError as exc:
            log.warning("web_push_fallback_scan_db_unavailable", error=str(exc))
            counters.fallback_scanned = 0
            await self._emit_heartbeat(_PHASE_IDLE, counters)
            return 0
        except Exception as exc:  # noqa: BLE001 - safety net must not crash
            if _is_db_down(exc):
                log.warning(
                    "web_push_fallback_scan_db_unavailable", error=str(exc)
                )
                counters.fallback_scanned = 0
                await self._emit_heartbeat(_PHASE_IDLE, counters)
                return 0
            # Unexpected non-DB error: log and swallow so one bad scan does
            # not kill the scheduler / supervised process; the next tick
            # (and the live loop) retry.
            log.error("web_push_fallback_scan_failed", error=str(exc))
            counters.fallback_scanned = 0
            await self._emit_heartbeat(_PHASE_IDLE, counters)
            return 0
        # The fallback scan re-scanned ``pushed_at IS NULL``; record how many
        # events it processed so an operator can see the safety net is doing
        # work (or correctly idle). Heartbeat is fail-soft, so it can never
        # crash the scheduler — preserving the "safety net must not crash"
        # contract end to end.
        counters.fallback_scanned = processed
        await self._emit_heartbeat(_PHASE_IDLE, counters)
        return processed

    async def run_backfill(
        self, counters: _HeartbeatCounters | None = None
    ) -> int:
        """Deliver every ``events`` row with ``pushed_at IS NULL`` (id ASC).

        ``counters`` (optional) is the current cycle's heartbeat tally; when
        supplied each delivered / content-error-stamped event increments it
        so the caller can report an accurate ``counters`` block.  It is
        threaded purely so existing callers/tests that don't care about the
        heartbeat keep working unchanged (defaults to ``None`` ⇒ no tally).

        Returns:
            The number of events processed.
        """
        async with session_scope() as session:
            pending = await self._fetch_pending_event_ids(session)
        log.info("web_push_backfill", pending=len(pending))
        for event_id in pending:
            await self._safe_deliver(event_id, counters=counters)
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

    async def _stream_forever(self, *, initial: bool) -> None:
        """LISTEN, then backfill, then consume; reconnect with mini-backfill.

        Each iteration: enter ``pg_listen_iter`` (attaches ``LISTEN`` and
        starts asyncpg buffering NOTIFYs) → run the (mini-)backfill while the
        listener is already live → drain the stream.  On a connection drop we
        loop: the next iteration's mini-backfill recovers anything committed
        during the gap *before* re-entering the loop (P1-7).  ``initial`` only
        changes the log label; the recovery query is identical.
        """
        first = initial
        while True:
            # One heartbeat cycle per loop iteration (PRD §7.1 liveness): the
            # (mini-)backfill below is the ``sending`` phase and tallies what
            # it delivered; once it parks on the NOTIFY stream the process is
            # ``idle``. Every ``_emit_heartbeat`` call is fail-soft, so a bad
            # heartbeat write can never break this reconnect/drain loop.
            counters = _HeartbeatCounters()
            try:
                async with pg_listen_iter(EVENTS_NOTIFY_CHANNEL) as notes:
                    # LISTEN is attached *now* (pg_listen_iter.__aenter__
                    # called add_listener before yielding); asyncpg's read
                    # loop buffers every NOTIFY committed from here on into
                    # the wrapper queue — including any that land *during*
                    # the backfill below. Run the backfill only after this.
                    if first:
                        log.info(
                            "web_push_listening_then_backfill",
                            channel=EVENTS_NOTIFY_CHANNEL,
                        )
                        await self.run_backfill(counters=counters)
                        first = False
                    else:
                        log.info(
                            "web_push_reconnect_mini_backfill",
                            channel=EVENTS_NOTIFY_CHANNEL,
                        )
                        await self.run_mini_backfill(counters=counters)
                    # Report the drain just done (phase=sending), then signal
                    # we are about to park on the live stream (phase=idle).
                    # Both are best-effort — see _emit_heartbeat.
                    await self._emit_heartbeat(_PHASE_SENDING, counters)
                    log.info(
                        "web_push_listening", channel=EVENTS_NOTIFY_CHANNEL
                    )
                    await self._emit_heartbeat(_PHASE_IDLE, counters)
                    async for note in notes:
                        await self._handle_notification(note.payload)
            except asyncio.CancelledError:
                log.info("web_push_sender_cancelled")
                raise
            except PushConfigError:
                # Bad VAPID — retrying the same key just hammers the push
                # service; halt so the operator fixes config (I4).
                log.error("web_push_halt_bad_vapid")
                raise
            except Exception as exc:  # noqa: BLE001 - resilient reconnect loop
                log.warning("web_push_stream_lost", error=str(exc))
                # Still emit a per-iteration liveness heartbeat even when the
                # stream/DB is flapping (phase=idle, whatever was delivered
                # before the drop). Fail-soft, so a heartbeat error here can
                # never stop the reconnect backoff/retry.
                await self._emit_heartbeat(_PHASE_IDLE, counters)
                await asyncio.sleep(_RETRY_BACKOFF_BASE_SECONDS)

    async def _handle_notification(self, payload: str) -> None:
        """Parse a NOTIFY payload (an int event id) and deliver that event."""
        try:
            event_id = int(payload)
        except (TypeError, ValueError):
            log.warning("web_push_bad_notify_payload", payload=payload)
            return
        await self._safe_deliver(event_id)

    async def _safe_deliver(
        self,
        event_id: int,
        counters: _HeartbeatCounters | None = None,
    ) -> None:
        """Deliver one event by id; never let one *event* failure kill the loop.

        A normal per-event error is logged and swallowed so the next event /
        next backfill proceeds.  :class:`PushConfigError` (bad VAPID, I4) is
        the deliberate exception: it is *not* swallowed — it propagates up
        through backfill / the stream loop so delivery halts instead of the
        config bug hiding as silently-"ok" stamped events.

        ``counters`` (optional) is the current heartbeat cycle's tally,
        forwarded to :meth:`_deliver_event` so a real delivery / a
        content-error stamp is reflected in the next heartbeat.  An
        idempotent skip (already-pushed) and a per-event failure do *not*
        bump the tally — neither delivered the event this cycle.
        """
        try:
            async with session_scope() as session:
                row = await session.get(Event, event_id)
                if row is None:
                    log.warning("web_push_event_missing", event_id=event_id)
                    return
                if row.pushed_at is not None:
                    # Already delivered (backfill/live race) — idempotent skip.
                    return
                await self._deliver_event(session, row, counters=counters)
        except PushConfigError:
            # Bad VAPID config — must halt delivery, not be swallowed (I4).
            raise
        except Exception as exc:  # noqa: BLE001 - one event never kills loop
            log.error(
                "web_push_event_failed", event_id=event_id, error=str(exc)
            )

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _deliver_event(
        self,
        session: AsyncSession,
        event: Event,
        counters: _HeartbeatCounters | None = None,
    ) -> None:
        """Fan one event out to every subscription, then stamp ``pushed_at``.

        Non-push events (``payload["push"] is None``) are stamped and
        skipped — no ``webpush`` call.  If *any* subscription hits an
        exhausted transient failure the event is left ``pushed_at IS NULL``
        so the next backfill retries it.

        ``counters`` (optional): when supplied, the *same* terminal
        classification this method already computes for logging
        (``any_real_delivery`` vs the durable-but-zero-delivery
        content-error branch) bumps the heartbeat tally — ``delivered`` on a
        real 2xx, ``content_error`` on the content-error-stamped case.  A
        non-push stamp, a no-subscriptions stamp and a deferred event are
        *not* counted (no push was actually delivered this cycle).
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

        # I6: fan out concurrently but bounded. A serial ``await`` loop lets
        # one slow endpoint head-of-line block every other subscription (and
        # the whole event loop); an unbounded ``gather`` would spawn one
        # ``to_thread`` worker per subscription. A semaphore caps in-flight
        # blocking threads at _FANOUT_CONCURRENCY while keeping fan-out
        # parallel. ``return_exceptions=True`` so one crashing sub never
        # aborts the others — except PushConfigError, which we re-raise to
        # halt delivery (bad VAPID; I4).
        sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)

        async def _bounded(sub: PushSubscription) -> str:
            async with sem:
                return await self._deliver_to_subscription(
                    session, sub, data
                )

        results = await asyncio.gather(
            *(_bounded(sub) for sub in subs), return_exceptions=True
        )

        # ``all_durable`` — every subscription reached a terminal state (no
        # "transient" / unexpected error left), so the event must NOT be
        # retried and ``pushed_at`` is stamped.  ``content_error`` (413 /
        # other non-gone 4xx) is *intentionally* durable: the identical
        # oversized/malformed body will always fail, so stamping prevents an
        # infinite backfill retry loop (R13 #1 — KEEP this).
        #
        # ``any_real_delivery`` — true only when at least one subscription
        # returned ``"ok"`` (a genuine 2xx).  Without this, an event whose
        # every subscription returned ``content_error`` would still be
        # stamped *and* logged as ``web_push_event_delivered`` with
        # ``subscriptions=len(subs)`` — misleading operators that devices
        # received the push when in fact zero did (R13 #1).  When the event
        # is durable but no real delivery happened we still stamp (no
        # behaviour change) but emit a *distinct*
        # ``web_push_event_content_error_stamped`` event so operators are
        # not told a delivery occurred.
        all_durable = True
        any_real_delivery = False
        for sub, outcome in zip(subs, results, strict=True):
            if isinstance(outcome, PushConfigError):
                # Bad VAPID — halt delivery; do NOT stamp the event.
                raise outcome
            if isinstance(outcome, BaseException):
                # Any other unexpected error for one sub: log, treat as
                # transient so the event retries (never silently "ok").
                log.error(
                    "web_push_subscription_error",
                    event_id=event.id,
                    subscription_id=str(sub.id),
                    error=str(outcome),
                )
                all_durable = False
                continue
            if outcome == "transient":
                all_durable = False
            elif outcome == "ok":
                any_real_delivery = True

        if all_durable:
            event.pushed_at = _now()
            if any_real_delivery:
                if counters is not None:
                    counters.delivered += 1
                log.info(
                    "web_push_event_delivered",
                    event_id=event.id,
                    event_type=event.event_type,
                    subscriptions=len(subs),
                )
            else:
                # Durable (so not retried) but ZERO devices actually
                # received it — every sub was content_error / gone. Stamp
                # is intentional (no infinite retry); the distinct event
                # name stops operators reading this as a real delivery.
                if counters is not None:
                    counters.content_error += 1
                log.warning(
                    "web_push_event_content_error_stamped",
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
        """Deliver to one subscription with bounded transient retry (I4).

        Returns one of:

        * ``"ok"`` — 2xx, ``last_used_at`` stamped (a *real* delivery);
        * ``"gone"`` — 404/410, row deleted (dead device);
        * ``"content_error"`` — 413 Payload Too Large: never a real
          delivery and not retryable (same body always 413), but the event
          *is* stamped so it is not retried forever;
        * ``"transient"`` — 5xx / network, retries exhausted (event must
          NOT be marked pushed — next backfill retries).

        Raises:
            PushConfigError: on 401/403 — bad VAPID auth halts the whole
                delivery (every sub would fail identically; silently
                stamping would lose the event).
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
                if status in _VAPID_AUTH_STATUSES:
                    # 401/403: the push service rejects our VAPID key/claims.
                    # This is a config fault that would fail *every*
                    # subscription identically — surface it so delivery halts
                    # and the operator fixes VAPID, never stamp "ok".
                    log.error(
                        "web_push_vapid_auth_error",
                        subscription_id=str(sub_id),
                        status=status,
                    )
                    raise PushConfigError(
                        f"push service rejected VAPID auth (HTTP {status}); "
                        "check settings.vapid_private_key / claims"
                    ) from exc
                if status == _CONTENT_TOO_LARGE_STATUS:
                    # 413: the encrypted body is too large for this service.
                    # Not retryable (identical body always 413) and never a
                    # real delivery — distinct outcome: stamp so it is not
                    # retried forever, but do NOT count it as "ok".
                    log.error(
                        "web_push_content_too_large",
                        subscription_id=str(sub_id),
                        status=status,
                    )
                    return "content_error"
                if status is not None and 400 <= status < 500:
                    # Other 4xx (e.g. malformed request): not retryable and
                    # not "gone"; never a real delivery. Treat like 413 —
                    # stamp so a single permanently-bad sub never blocks the
                    # event forever, but do NOT count it as a delivery.
                    log.error(
                        "web_push_client_error",
                        subscription_id=str(sub_id),
                        status=status,
                    )
                    return "content_error"
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
        """Compute *today's* realised PnL / fills digest and emit the event.

        P1-8: the push body says "今日" (today), so the aggregate must be
        date-scoped to *today in* ``settings.tz`` — previously it summed
        ALL-time ``my_trades`` so the number contradicted the label.

        Aggregates ``my_trades`` filled today: ``fill_status == 'filled'``
        **and** ``opened_at >= start-of-today`` (local midnight in
        ``settings.tz``, converted to a tz-aware UTC bound for the
        ``TIMESTAMPTZ`` column — ``my_trades`` has no ``filled_at``; the BUY
        ``opened_at`` is the fill timestamp).  Realised PnL is
        ``SUM(pnl_usd)`` over those rows; fills is the row count.  Emits a
        ``DAILY_PNL_SUMMARY`` event with a Chinese-language
        :class:`PushPayload`; the normal delivery loop ships it.

        Returns:
            The new ``events.id``.
        """
        today_start = _start_of_today(self._settings.tz)
        async with session_scope() as session:
            pnl_row = await session.execute(
                select(
                    func.coalesce(func.sum(MyTrade.pnl_usd), 0),
                    func.count(MyTrade.id),
                )
                .where(MyTrade.fill_status == "filled")
                .where(MyTrade.opened_at >= today_start)
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
