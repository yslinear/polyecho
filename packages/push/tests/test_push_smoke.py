"""
Tests for ``polyecho.push`` (web-push-sender, Phase-5 Wave-2).

Two layers:

* **Import / contract guards** — package imports, ``__main__.main`` is a
  coroutine, the documented PRD §5.8 event-type constants are intact.
* **Delivery behaviour** — the Wave-2 contract, exercised against a fake
  in-memory async session + a mocked ``webpush`` (NO network, NO Postgres,
  mirroring the shared package's no-network test discipline):

  (a) 404 *and* 410 each DELETE the offending subscription;
  (b) 2xx stamps the sub's ``last_used_at`` + the event's ``pushed_at``;
  (c) a non-push event (``push is None``) is stamped, no ``webpush`` call;
  (d) ``--once`` drains all unpushed events then exits 0;
  (e) startup backfill SELECTs only ``pushed_at IS NULL`` (id ASC);
  (f) transient 5xx → event NOT marked pushed (so backfill retries).
"""

from __future__ import annotations

import asyncio
import importlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import / contract guards
# ---------------------------------------------------------------------------


def test_package_imports() -> None:
    pkg = importlib.import_module("polyecho.push")
    assert hasattr(pkg, "WebPushSender")
    assert pkg.__all__ == ["WebPushSender"]


def test_entrypoint_module_imports() -> None:
    """``python -m polyecho.push`` entry module imports and exposes main()."""
    mod = importlib.import_module("polyecho.push.__main__")
    assert asyncio.iscoroutinefunction(mod.main)


def test_event_type_constants_and_set() -> None:
    """The seven PRD §5.8 event-type constants are intact (back-compat)."""
    from polyecho.push import sender as s

    expected = {
        "new_trade",
        "position_closed",
        "daily_pnl_summary",
        "risk_triggered",
        "system_error",
        "new_leader_candidate",
        "pending_action",
    }
    assert s.EVENT_NEW_TRADE == "new_trade"
    assert s.EVENT_POSITION_CLOSED == "position_closed"
    assert s.EVENT_DAILY_PNL_SUMMARY == "daily_pnl_summary"
    assert s.EVENT_RISK_TRIGGERED == "risk_triggered"
    assert s.EVENT_SYSTEM_ERROR == "system_error"
    assert s.EVENT_NEW_LEADER_CANDIDATE == "new_leader_candidate"
    assert s.EVENT_PENDING_ACTION == "pending_action"
    assert s.ALL_EVENT_TYPES == frozenset(expected)
    assert isinstance(s.ALL_EVENT_TYPES, frozenset)


def test_sender_instantiates_with_patched_settings() -> None:
    with patch("polyecho.push.sender.get_settings", return_value=MagicMock()):
        from polyecho.push.sender import WebPushSender

        sender = WebPushSender()
    assert sender is not None


# ---------------------------------------------------------------------------
# Test doubles: a minimal fake AsyncSession that records ORM mutations
# ---------------------------------------------------------------------------


class _FakeEvent:
    def __init__(self, event_id: int, payload: dict[str, Any] | None) -> None:
        self.id = event_id
        self.event_type = "copy_fill"
        self.severity = "info"
        self.payload = payload
        self.pushed_at: datetime | None = None


class _FakeSub:
    def __init__(self, endpoint: str) -> None:
        self.id = uuid.uuid4()
        self.endpoint = endpoint
        self.p256dh_key = "p256dh-xxxx"
        self.auth_key = "auth-xxxx"
        self.last_used_at: datetime | None = None


class _FakeResult:
    def __init__(self, rows: list[Any], *, scalar_rows: bool = True) -> None:
        self._rows = rows
        self._scalar = scalar_rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one(self) -> Any:
        return self._rows[0]


class _FakeSession:
    """Records ``delete``/``update`` statements and serves ``get``/``execute``.

    Only the small surface ``WebPushSender`` uses is implemented; this keeps
    the suite free of Postgres while still asserting the real SQL intent
    (which table a DELETE/UPDATE targets, what values an UPDATE sets).
    """

    def __init__(
        self,
        events: dict[int, _FakeEvent] | None = None,
        subs: list[_FakeSub] | None = None,
    ) -> None:
        self._events = events or {}
        self._subs = subs if subs is not None else []
        self.deleted_subs: list[uuid.UUID] = []
        self.updated_last_used: list[uuid.UUID] = []

    async def get(self, _model: Any, pk: int) -> Any:
        return self._events.get(pk)

    @staticmethod
    def _bound_uuid(stmt: Any) -> uuid.UUID | None:
        """Pull the bound UUID param out of a compiled DELETE/UPDATE."""
        try:
            params = stmt.compile().params
        except Exception:  # pragma: no cover - defensive
            return None
        for value in params.values():
            if isinstance(value, uuid.UUID):
                return value
        return None

    async def execute(self, stmt: Any) -> _FakeResult:
        text = str(stmt).lower()
        if text.startswith("delete"):
            sub_id = self._bound_uuid(stmt)
            if sub_id is not None:
                self.deleted_subs.append(sub_id)
                self._subs = [s for s in self._subs if s.id != sub_id]
            return _FakeResult([])
        if text.startswith("update"):
            sub_id = self._bound_uuid(stmt)
            if sub_id is not None:
                self.updated_last_used.append(sub_id)
                for s in self._subs:
                    if s.id == sub_id:
                        s.last_used_at = datetime.now()
            return _FakeResult([])
        # SELECT
        if "push_subscriptions" in text:
            return _FakeResult(list(self._subs))
        if "count" in text:  # daily-summary aggregate
            return _FakeResult([(0, 0)])
        # events id list ordered by id asc, pushed_at IS NULL
        pending = sorted(
            e.id for e in self._events.values() if e.pushed_at is None
        )
        return _FakeResult(pending)


@pytest.fixture
def _patched_scope() -> Any:
    """Patch ``session_scope`` in sender to yield our fake session."""

    def _make(session: _FakeSession) -> Any:
        @asynccontextmanager
        async def _scope() -> Any:
            yield session

        return _scope

    return _make


def _make_sender() -> Any:
    settings = MagicMock()
    settings.vapid_private_key.get_secret_value.return_value = "priv-redacted"
    with patch("polyecho.push.sender.get_settings", return_value=settings):
        from polyecho.push.sender import WebPushSender

        return WebPushSender()


def _webpush_exc(status: int) -> Exception:
    from pywebpush import WebPushException  # type: ignore[import-untyped]

    resp = MagicMock()
    resp.status_code = status
    exc: Exception = WebPushException("push failed", response=resp)
    return exc


# ---------------------------------------------------------------------------
# (a) 404 & 410 each delete the offending subscription
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.asyncio
async def test_gone_status_deletes_subscription(
    status: int, _patched_scope: Any
) -> None:
    ev = _FakeEvent(1, {"data": {}, "push": {"title": "t", "body": "b"}})
    sub = _FakeSub("https://push.example/abc")
    session = _FakeSession({1: ev}, [sub])
    sender = _make_sender()

    with (
        patch(
            "polyecho.push.sender.session_scope", _patched_scope(session)
        ),
        patch(
            "polyecho.push.sender.webpush",
            side_effect=_webpush_exc(status),
        ),
        patch("polyecho.push.sender.build_vapid_claims", return_value={"sub": "x"}),
    ):
        await sender._safe_deliver(1)

    assert session.deleted_subs == [sub.id]
    # Event still delivered durably (the only sub was gone, not transient).
    assert ev.pushed_at is not None


# ---------------------------------------------------------------------------
# (b) 2xx stamps last_used_at + event pushed_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_stamps_last_used_and_pushed_at(
    _patched_scope: Any,
) -> None:
    ev = _FakeEvent(7, {"data": {}, "push": {"title": "t", "body": "b"}})
    sub = _FakeSub("https://push.example/ok")
    session = _FakeSession({7: ev}, [sub])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush", return_value=None) as wp,
        patch("polyecho.push.sender.build_vapid_claims", return_value={"sub": "x"}),
    ):
        await sender._safe_deliver(7)

    wp.assert_called_once()
    assert session.updated_last_used == [sub.id]
    assert ev.pushed_at is not None


# ---------------------------------------------------------------------------
# (c) non-push event (push=None): stamped, no webpush call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_push_event_stamped_no_webpush(_patched_scope: Any) -> None:
    ev = _FakeEvent(3, {"data": {"x": 1}, "push": None})
    session = _FakeSession({3: ev}, [_FakeSub("https://push.example/n")])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush") as wp,
    ):
        await sender._safe_deliver(3)

    wp.assert_not_called()
    assert ev.pushed_at is not None


# ---------------------------------------------------------------------------
# (d) --once drains all unpushed events then exits 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_once_drains_all_and_exits_zero(_patched_scope: Any) -> None:
    events = {
        1: _FakeEvent(1, {"data": {}, "push": None}),
        2: _FakeEvent(2, {"data": {}, "push": None}),
    }
    session = _FakeSession(events, [])
    sender = _make_sender()

    from polyecho.push import __main__ as entry

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch.object(entry, "WebPushSender", return_value=sender),
        patch("polyecho.push.__main__.configure_logging"),
        patch(
            "polyecho.push.__main__.get_settings",
            return_value=MagicMock(log_level="INFO", datasource="fixture"),
        ),
    ):
        rc = await entry.main(["--once"])

    assert rc == 0
    assert all(e.pushed_at is not None for e in events.values())


@pytest.mark.asyncio
async def test_once_exits_zero_when_db_unreachable() -> None:
    """CI-smoke: no Postgres → ``--once`` logs and returns 0, never crashes."""
    sender = _make_sender()

    @asynccontextmanager
    async def _boom() -> Any:
        raise OSError("Connect call failed ('127.0.0.1', 5432)")
        yield  # pragma: no cover

    with patch("polyecho.push.sender.session_scope", _boom):
        processed = await sender.run_once()

    assert processed == 0


# ---------------------------------------------------------------------------
# (e) startup backfill selects only pushed_at IS NULL (id ASC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_selects_only_unpushed(_patched_scope: Any) -> None:
    already = _FakeEvent(1, {"data": {}, "push": None})
    already.pushed_at = datetime.now()
    pending_a = _FakeEvent(2, {"data": {}, "push": None})
    pending_b = _FakeEvent(3, {"data": {}, "push": None})
    session = _FakeSession(
        {1: already, 2: pending_a, 3: pending_b}, []
    )
    sender = _make_sender()

    delivered: list[int] = []
    orig = sender._safe_deliver

    async def _spy(event_id: int) -> None:
        delivered.append(event_id)
        await orig(event_id)

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch.object(sender, "_safe_deliver", _spy),
    ):
        count = await sender.run_backfill()

    # Only the two unpushed ids, ascending; the already-pushed one excluded.
    assert delivered == [2, 3]
    assert count == 2
    assert already.pushed_at is not None  # untouched


# ---------------------------------------------------------------------------
# (f) transient 5xx → event NOT marked pushed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_5xx_not_marked_pushed(_patched_scope: Any) -> None:
    ev = _FakeEvent(9, {"data": {}, "push": {"title": "t", "body": "b"}})
    sub = _FakeSub("https://push.example/flaky")
    session = _FakeSession({9: ev}, [sub])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch(
            "polyecho.push.sender.webpush",
            side_effect=_webpush_exc(503),
        ),
        patch("polyecho.push.sender.build_vapid_claims", return_value={"sub": "x"}),
        patch("polyecho.push.sender.asyncio.sleep", return_value=None),
    ):
        await sender._safe_deliver(9)

    assert ev.pushed_at is None  # deferred for next backfill
    assert session.deleted_subs == []  # 5xx is not "gone"


# ---------------------------------------------------------------------------
# Daily PnL summary emits a DAILY_PNL_SUMMARY event via emit_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_pnl_summary_emits_event(_patched_scope: Any) -> None:
    session = _FakeSession({}, [])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch(
            "polyecho.push.sender.emit_event", return_value=42
        ) as emit,
    ):
        event_id = await sender.emit_daily_pnl_summary()

    assert event_id == 42
    emit.assert_awaited_once()
    kwargs = emit.call_args.kwargs
    from polyecho.shared.events import EventType

    assert kwargs["event_type"] == EventType.DAILY_PNL_SUMMARY
    assert kwargs["push"].title == "每日跟單摘要"


# ---------------------------------------------------------------------------
# P1-7: LISTEN attaches BEFORE the startup backfill — an event committed
# during the backfill (queued in the asyncpg NOTIFY buffer) is still
# delivered, not stranded until restart.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listen_attaches_before_backfill_no_lost_event() -> None:
    """Order proof: ``pg_listen_iter.__aenter__`` (LISTEN) runs strictly
    before ``run_backfill``; a NOTIFY arriving during backfill is buffered
    by the (already-attached) listener and delivered from the stream.
    """
    sender = _make_sender()

    order: list[str] = []
    # The bounded buffer the (already-attached) listener feeds. We push a
    # "gap" event id into it the instant LISTEN is attached, simulating an
    # event committed during the backfill window.
    buffer: asyncio.Queue[Any] = asyncio.Queue()

    class _Note:
        def __init__(self, payload: str) -> None:
            self.payload = payload

    @asynccontextmanager
    async def _fake_listen(_channel: str) -> Any:
        order.append("listen_attached")
        # Event 99 was committed in the gap → NOTIFY queued *now*.
        await buffer.put(_Note("99"))

        async def _gen() -> Any:
            while True:
                note = await buffer.get()
                yield note

        gen = _gen()
        try:
            yield gen
        finally:
            await gen.aclose()

    delivered: list[int] = []

    async def _fake_backfill() -> int:
        order.append("backfill_ran")
        # The gap event is NOT visible to the backfill snapshot (committed
        # after its SELECT) — but it is already sitting in the buffer.
        return 0

    async def _fake_safe(event_id: int) -> None:
        delivered.append(event_id)
        if event_id == 99:
            raise asyncio.CancelledError  # stop the infinite stream

    with (
        patch("polyecho.push.sender.pg_listen_iter", _fake_listen),
        patch.object(sender, "run_backfill", _fake_backfill),
        patch.object(sender, "_safe_deliver", _fake_safe),
    ):
        with pytest.raises(asyncio.CancelledError):
            await sender.run()

    # LISTEN attached strictly before the backfill ran (P1-7 ordering).
    assert order == ["listen_attached", "backfill_ran"]
    # The gap event (committed during backfill) was still delivered.
    assert 99 in delivered


# ---------------------------------------------------------------------------
# P1-7: a stream-loss reconnect runs a mini-backfill BEFORE re-entering the
# LISTEN loop, so events missed during the disconnect are recovered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_runs_mini_backfill_before_listen_loop() -> None:
    sender = _make_sender()
    order: list[str] = []
    attempts = {"n": 0}

    @asynccontextmanager
    async def _fake_listen(_channel: str) -> Any:
        attempts["n"] += 1
        order.append(f"listen_{attempts['n']}")

        async def _gen() -> Any:
            if attempts["n"] == 1:
                # First connection drops immediately.
                raise RuntimeError("stream lost")
                yield  # pragma: no cover
            # Second connection: stop the loop after mini-backfill ran.
            raise asyncio.CancelledError
            yield  # pragma: no cover

        gen = _gen()
        try:
            yield gen
        finally:
            with __import__("contextlib").suppress(Exception):
                await gen.aclose()

    async def _fake_backfill() -> int:
        order.append("startup_backfill")
        return 0

    async def _fake_mini() -> int:
        order.append("mini_backfill")
        return 0

    with (
        patch("polyecho.push.sender.pg_listen_iter", _fake_listen),
        patch.object(sender, "run_backfill", _fake_backfill),
        patch.object(sender, "run_mini_backfill", _fake_mini),
        patch("polyecho.push.sender.asyncio.sleep", return_value=None),
    ):
        with pytest.raises(asyncio.CancelledError):
            await sender.run()

    # 1st conn: LISTEN then startup backfill; drop → reconnect; 2nd conn:
    # LISTEN then *mini*-backfill BEFORE the (cancelled) listen loop.
    assert order == [
        "listen_1",
        "startup_backfill",
        "listen_2",
        "mini_backfill",
    ]


# ---------------------------------------------------------------------------
# P1-8: the daily summary counts ONLY today's (settings.tz) filled trades.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_summary_filters_today_only() -> None:
    """The aggregate query carries an ``opened_at >= start-of-today`` bound
    (in addition to ``fill_status == 'filled'``) so the number matches the
    "今日" label — previously it summed ALL-time trades.
    """
    captured: dict[str, str] = {}

    class _CapSession:
        async def execute(self, stmt: Any) -> Any:
            captured["sql"] = str(stmt.compile(
                compile_kwargs={"literal_binds": False}
            )).lower()
            return _FakeResult([(0, 0)])

    @asynccontextmanager
    async def _scope() -> Any:
        yield _CapSession()

    settings = MagicMock()
    settings.tz = "Asia/Taipei"
    settings.vapid_private_key.get_secret_value.return_value = "p"
    with patch("polyecho.push.sender.get_settings", return_value=settings):
        from polyecho.push.sender import WebPushSender

        sender = WebPushSender()

    with (
        patch("polyecho.push.sender.session_scope", _scope),
        patch("polyecho.push.sender.emit_event", return_value=1),
    ):
        await sender.emit_daily_pnl_summary()

    sql = captured["sql"]
    assert "fill_status" in sql
    # The today bound is present (opened_at compared to a lower bound).
    assert "opened_at" in sql and ">=" in sql


def test_start_of_today_is_tz_aware_local_midnight() -> None:
    """``_start_of_today`` returns the UTC instant of local midnight."""
    from datetime import UTC

    from polyecho.push.sender import _start_of_today

    bound = _start_of_today("Asia/Taipei")
    assert bound.tzinfo is not None
    # Asia/Taipei is UTC+8 with no DST → local midnight == 16:00 UTC prev day.
    assert bound.astimezone(UTC).hour == 16
    # Bad tz string falls back to UTC midnight (no crash).
    utc_bound = _start_of_today("Not/AZone")
    assert utc_bound.hour == 0 and utc_bound.tzinfo is not None


# ---------------------------------------------------------------------------
# I4: 401/403 halt delivery (PushConfigError, NOT silently "ok"); 413 is a
# distinct content_error (stamped, never a real delivery); 404/410 still
# delete; 2xx still stamps; 5xx still unstamped-retry (regression coverage
# for the latter three is above — here the new I4 cases).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_vapid_auth_status_halts_delivery(
    status: int, _patched_scope: Any
) -> None:
    """401/403 raises PushConfigError (halts) and does NOT stamp the event
    nor return a silent "ok"."""
    from polyecho.push.sender import PushConfigError

    ev = _FakeEvent(1, {"data": {}, "push": {"title": "t", "body": "b"}})
    sub = _FakeSub("https://push.example/badvapid")
    session = _FakeSession({1: ev}, [sub])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch(
            "polyecho.push.sender.webpush",
            side_effect=_webpush_exc(status),
        ),
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
    ):
        # _safe_deliver must NOT swallow PushConfigError (it must halt).
        with pytest.raises(PushConfigError):
            await sender._safe_deliver(1)

    assert ev.pushed_at is None  # never stamped — not a real delivery
    assert session.deleted_subs == []  # 401/403 is not "gone"


@pytest.mark.asyncio
async def test_413_is_distinct_content_error_stamped_not_delivery(
    _patched_scope: Any,
) -> None:
    """413 stamps the event (so it is not retried forever) but is a distinct
    outcome — it must NOT be counted as a real delivery (no last_used_at)."""
    ev = _FakeEvent(2, {"data": {}, "push": {"title": "t", "body": "b" * 5}})
    sub = _FakeSub("https://push.example/toolarge")
    session = _FakeSession({2: ev}, [sub])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch(
            "polyecho.push.sender.webpush",
            side_effect=_webpush_exc(413),
        ),
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
    ):
        await sender._safe_deliver(2)

    # Stamped so it is not retried forever (distinct from 5xx transient)...
    assert ev.pushed_at is not None
    # ...but never a real delivery: last_used_at NOT stamped, not deleted.
    assert session.updated_last_used == []
    assert session.deleted_subs == []


# ---------------------------------------------------------------------------
# R13 #1: when EVERY subscription returns content_error the event is still
# stamped (KEEP — prevents an infinite retry of an un-deliverable body) but
# it must NOT be logged as web_push_event_delivered; a distinct
# web_push_event_content_error_stamped is logged and `any_real_delivery`
# stays false (no operator should read "delivered" when zero devices got it).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_content_error_stamps_but_logs_distinct_not_delivered(
    _patched_scope: Any,
) -> None:
    """All-413 event: pushed_at stamped (no infinite retry) but the log
    event is the distinct content-error one, NOT `_delivered`, and no real
    delivery was recorded (no last_used_at, nothing deleted)."""
    ev = _FakeEvent(11, {"data": {}, "push": {"title": "t", "body": "b" * 9}})
    subs = [
        _FakeSub("https://push.example/big1"),
        _FakeSub("https://push.example/big2"),
    ]
    session = _FakeSession({11: ev}, subs)
    sender = _make_sender()

    events_logged: list[str] = []

    def _capture(event: str, **_kw: Any) -> None:
        events_logged.append(event)

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch(
            "polyecho.push.sender.webpush",
            side_effect=_webpush_exc(413),
        ),
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
        patch.object(sender, "_settings"),
        patch("polyecho.push.sender.log.info", side_effect=_capture),
        patch("polyecho.push.sender.log.warning", side_effect=_capture),
        patch("polyecho.push.sender.log.error", side_effect=_capture),
    ):
        await sender._safe_deliver(11)

    # Stamped (intentional — KEEP: an oversized body always 413s, so not
    # retrying forever is correct)...
    assert ev.pushed_at is not None
    # ...but the misleading "delivered" event must NOT have been logged...
    assert "web_push_event_delivered" not in events_logged
    # ...the distinct content-error event WAS logged instead...
    assert "web_push_event_content_error_stamped" in events_logged
    # ...and zero real deliveries happened (no last_used_at, none deleted).
    assert session.updated_last_used == []
    assert session.deleted_subs == []


@pytest.mark.asyncio
async def test_mixed_ok_and_content_error_still_logs_delivered(
    _patched_scope: Any,
) -> None:
    """If at least one sub returns a real 2xx ("ok"), the event IS a real
    delivery → the normal `web_push_event_delivered` event is logged (the
    distinct content-error event is reserved for the zero-real-delivery
    case only)."""
    ev = _FakeEvent(12, {"data": {}, "push": {"title": "t", "body": "b"}})
    ok_sub = _FakeSub("https://push.example/ok")
    big_sub = _FakeSub("https://push.example/big")
    session = _FakeSession({12: ev}, [ok_sub, big_sub])
    sender = _make_sender()

    def _webpush(*, subscription_info: Any, **_kw: Any) -> None:
        if subscription_info["endpoint"].endswith("/big"):
            raise _webpush_exc(413)
        return None

    events_logged: list[str] = []

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush", side_effect=_webpush),
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
        patch(
            "polyecho.push.sender.log.info",
            side_effect=lambda e, **_k: events_logged.append(e),
        ),
        patch(
            "polyecho.push.sender.log.warning",
            side_effect=lambda e, **_k: events_logged.append(e),
        ),
    ):
        await sender._safe_deliver(12)

    assert ev.pushed_at is not None
    # A real delivery happened (the ok sub) → normal delivered event...
    assert "web_push_event_delivered" in events_logged
    # ...and the content-error-only event must NOT fire when any "ok" exists.
    assert "web_push_event_content_error_stamped" not in events_logged
    assert ok_sub.id in session.updated_last_used


# ---------------------------------------------------------------------------
# R13 #3 / R2: a dropped NOTIFY (pg_listen_iter buffer overflow) leaves an
# event pushed_at IS NULL with no reconnect/restart to ever retry it. The
# periodic fallback scan re-delivers such an event independent of stream
# activity; it is idempotent (already-pushed events are skipped) and
# best-effort (DB-down does not crash it).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_scan_delivers_event_with_no_notify(
    _patched_scope: Any,
) -> None:
    """An event sitting pushed_at IS NULL (its NOTIFY was dropped on buffer
    overflow — no stream event, no restart) is eventually delivered purely
    by the periodic fallback scan."""
    stranded = _FakeEvent(
        21, {"data": {}, "push": {"title": "t", "body": "b"}}
    )
    sub = _FakeSub("https://push.example/stranded")
    session = _FakeSession({21: stranded}, [sub])
    sender = _make_sender()

    assert stranded.pushed_at is None  # NOTIFY was dropped; never delivered

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush", return_value=None) as wp,
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
    ):
        # No NOTIFY, no reconnect — only the timer-driven safety net runs.
        processed = await sender.run_fallback_scan()

    assert processed == 1
    wp.assert_called_once()
    assert stranded.pushed_at is not None  # rescued by the fallback scan
    assert session.updated_last_used == [sub.id]


@pytest.mark.asyncio
async def test_fallback_scan_is_idempotent_skips_already_pushed(
    _patched_scope: Any,
) -> None:
    """The fallback scan reuses the backfill query + the pushed_at IS NOT
    NULL skip, so an already-delivered event is a no-op (no double push)."""
    done = _FakeEvent(31, {"data": {}, "push": {"title": "t", "body": "b"}})
    done.pushed_at = datetime.now()
    pending = _FakeEvent(32, {"data": {}, "push": None})
    session = _FakeSession({31: done, 32: pending}, [])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush") as wp,
    ):
        processed = await sender.run_fallback_scan()

    # Only the still-pending one is in the scan set; the done one untouched.
    assert processed == 1
    wp.assert_not_called()  # pending is a non-push event; done was skipped


@pytest.mark.asyncio
async def test_fallback_scan_db_down_returns_zero_no_crash() -> None:
    """Best-effort: a DB blip during a scan must not crash the supervised
    process — the scan no-ops (0) and the next tick / live loop retries."""
    sender = _make_sender()

    @asynccontextmanager
    async def _boom() -> Any:
        raise OSError("Connect call failed ('127.0.0.1', 5432)")
        yield  # pragma: no cover

    with patch("polyecho.push.sender.session_scope", _boom):
        processed = await sender.run_fallback_scan()

    assert processed == 0


def test_fallback_scan_interval_env_override_and_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interval is env-configurable (POLYECHO_PUSH_FALLBACK_SCAN_SECONDS)
    with a safe default and a floor so a typo can't make a busy-loop."""
    from polyecho.push import sender as smod

    monkeypatch.delenv("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS", raising=False)
    assert (
        smod._fallback_scan_interval_seconds()
        == smod._FALLBACK_SCAN_DEFAULT_SECONDS
    )

    monkeypatch.setenv("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS", "60")
    assert smod._fallback_scan_interval_seconds() == 60

    # Sub-floor / non-positive / garbage are all clamped or defaulted.
    monkeypatch.setenv("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS", "1")
    assert (
        smod._fallback_scan_interval_seconds()
        == smod._FALLBACK_SCAN_MIN_SECONDS
    )
    monkeypatch.setenv("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS", "not-an-int")
    assert (
        smod._fallback_scan_interval_seconds()
        == smod._FALLBACK_SCAN_DEFAULT_SECONDS
    )


@pytest.mark.asyncio
async def test_scheduler_registers_fallback_scan_job() -> None:
    """`_build_scheduler` wires BOTH the daily cron AND the interval-driven
    fallback scan; `--once` (which never builds the scheduler) is intact."""
    from polyecho.push import __main__ as entry

    sender = _make_sender()
    scheduler = entry._build_scheduler(sender, "Asia/Taipei")
    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "daily_pnl_summary" in job_ids
    assert "fallback_scan" in job_ids

    fb = scheduler.get_job("fallback_scan")
    # It is an interval trigger bound to the resolved seconds.
    from apscheduler.triggers.interval import (  # type: ignore[import-untyped]
        IntervalTrigger,
    )

    assert isinstance(fb.trigger, IntervalTrigger)
    # Its target is the safety-net coroutine, not the daily digest.
    assert fb.func == sender.run_fallback_scan


# ---------------------------------------------------------------------------
# I6: per-event fan-out is concurrent but bounded by the semaphore (a slow
# subscription does not head-of-line block the others, and at most
# _FANOUT_CONCURRENCY webpush threads run at once).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fanout_is_bounded_by_semaphore(_patched_scope: Any) -> None:
    from polyecho.push import sender as smod

    n_subs = smod._FANOUT_CONCURRENCY * 3
    ev = _FakeEvent(5, {"data": {}, "push": {"title": "t", "body": "b"}})
    subs = [_FakeSub(f"https://push.example/{i}") for i in range(n_subs)]
    session = _FakeSession({5: ev}, subs)
    sender = _make_sender()

    concurrent = 0
    peak = 0

    def _blocking(_sub: Any, _data: str) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        # Yield the GIL so other threads pile up if unbounded.
        import time

        time.sleep(0.005)
        concurrent -= 1

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch.object(sender, "_webpush_blocking", _blocking),
    ):
        await sender._safe_deliver(5)

    # All delivered, event stamped...
    assert ev.pushed_at is not None
    assert len(session.updated_last_used) == n_subs
    # ...and never more than the cap ran concurrently (proves the bound;
    # also proves it WAS concurrent — serial would peak at 1).
    assert peak <= smod._FANOUT_CONCURRENCY
    assert peak > 1
