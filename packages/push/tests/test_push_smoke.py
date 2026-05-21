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
        pending = sorted(e.id for e in self._events.values() if e.pushed_at is None)
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
async def test_gone_status_deletes_subscription(status: int, _patched_scope: Any) -> None:
    ev = _FakeEvent(1, {"data": {}, "push": {"title": "t", "body": "b"}})
    sub = _FakeSub("https://push.example/abc")
    session = _FakeSession({1: ev}, [sub])
    sender = _make_sender()

    with (
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
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
    session = _FakeSession({1: already, 2: pending_a, 3: pending_b}, [])
    sender = _make_sender()

    delivered: list[int] = []
    orig = sender._safe_deliver

    async def _spy(event_id: int, counters: Any = None) -> None:
        # Mirror the real _safe_deliver signature: run_backfill now forwards
        # an optional heartbeat ``counters`` tally. Pass it through to the
        # real method so behaviour (the pushed_at IS NULL ordering this test
        # asserts) is unchanged.
        delivered.append(event_id)
        await orig(event_id, counters=counters)

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
        patch("polyecho.push.sender.emit_event", return_value=42) as emit,
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

    async def _fake_backfill(counters: Any = None) -> int:  # noqa: ARG001
        order.append("backfill_ran")
        # The gap event is NOT visible to the backfill snapshot (committed
        # after its SELECT) — but it is already sitting in the buffer.
        # ``counters`` mirrors the real run_backfill signature (the
        # supervised loop now threads a heartbeat tally through it); this
        # double ignores it — the test only asserts call ordering.
        return 0

    async def _fake_safe(event_id: int, counters: Any = None) -> None:  # noqa: ARG001
        # Mirrors the real _safe_deliver signature (optional heartbeat
        # counters); the stream path delivers event 99 then cancels.
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

    async def _fake_backfill(counters: Any = None) -> int:  # noqa: ARG001
        order.append("startup_backfill")
        return 0

    async def _fake_mini(counters: Any = None) -> int:  # noqa: ARG001
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
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()
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
async def test_vapid_auth_status_halts_delivery(status: int, _patched_scope: Any) -> None:
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
    stranded = _FakeEvent(21, {"data": {}, "push": {"title": "t", "body": "b"}})
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
    assert smod._fallback_scan_interval_seconds() == smod._FALLBACK_SCAN_DEFAULT_SECONDS

    monkeypatch.setenv("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS", "60")
    assert smod._fallback_scan_interval_seconds() == 60

    # Sub-floor / non-positive / garbage are all clamped or defaulted.
    monkeypatch.setenv("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS", "1")
    assert smod._fallback_scan_interval_seconds() == smod._FALLBACK_SCAN_MIN_SECONDS
    monkeypatch.setenv("POLYECHO_PUSH_FALLBACK_SCAN_SECONDS", "not-an-int")
    assert smod._fallback_scan_interval_seconds() == smod._FALLBACK_SCAN_DEFAULT_SECONDS


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


# ---------------------------------------------------------------------------
# Heartbeat (FROZEN polyecho.shared.health contract, F0 — PRD §7.1/§2.3):
# the sender emits a well-formed ``push_heartbeat`` once per loop iteration
# and on ``--once``; counters = delivered / content_error / fallback_scanned
# this cycle; phase = idle/sending. STRICTLY fail-soft: a heartbeat-write
# error must NEVER abort the push loop, the ``--once`` backfill, or the
# scheduler.
#
# ``polyecho.shared.health`` lives OUTSIDE this worktree and is absent in an
# isolated build (so the production import is deliberately LAZY). These tests
# inject a fake module into ``sys.modules`` so the lazy
# ``from polyecho.shared.health import write_heartbeat`` resolves to our
# double — this also *proves* the lazy-import seam works regardless of
# whether the real shared module is present at import time.
# ---------------------------------------------------------------------------


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _fake_health(write_impl: Any) -> Any:
    """Inject a fake ``polyecho.shared.health`` exposing ``write_heartbeat``.

    Patches ``sys.modules`` so the sender's *lazy* in-method
    ``from polyecho.shared.health import write_heartbeat`` binds to
    ``write_impl``; restored on exit so no test leaks the fake.
    """
    import sys
    import types

    mod = types.ModuleType("polyecho.shared.health")
    mod.write_heartbeat = write_impl  # type: ignore[attr-defined]
    prev = sys.modules.get("polyecho.shared.health")
    sys.modules["polyecho.shared.health"] = mod
    try:
        yield
    finally:
        if prev is not None:
            sys.modules["polyecho.shared.health"] = prev
        else:
            sys.modules.pop("polyecho.shared.health", None)


def _hb_sender(*, datasource: str = "fixture") -> Any:
    """A sender whose settings carry the datasource flag the heartbeat
    payload must echo from ``polyecho.shared.config``. (``live_trading``
    was removed in R24 — the heartbeat seam now omits it.)"""
    settings = MagicMock()
    settings.vapid_private_key.get_secret_value.return_value = "priv-redacted"
    settings.datasource = datasource
    with patch("polyecho.push.sender.get_settings", return_value=settings):
        from polyecho.push.sender import WebPushSender

        return WebPushSender()


@pytest.mark.asyncio
async def test_once_writes_well_formed_heartbeat(_patched_scope: Any) -> None:
    """``--once`` emits exactly one ``write_heartbeat("push", ...)`` whose
    payload matches the FROZEN shape: phase, datasource, counters{} with
    the three cycle counts (F0 stamps ``ts`` itself; ``live_trading`` was
    retired in R24)."""

    ev = _FakeEvent(1, {"data": {}, "push": {"title": "t", "body": "b"}})
    sub = _FakeSub("https://push.example/ok")
    session = _FakeSession({1: ev}, [sub])
    sender = _hb_sender(datasource="live")

    calls: list[tuple[Any, ...]] = []

    def _wh(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    with (
        _fake_health(_wh),
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush", return_value=None),
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
    ):
        processed = await sender.run_once()

    assert processed == 1
    assert len(calls) == 1
    args, kwargs = calls[0]
    # F0's write_heartbeat is keyword-only after ``kind`` and stamps ``ts``
    # itself — the caller passes no ``ts``.
    assert args == ("push",)
    assert kwargs["phase"] in {"idle", "sending"}
    assert kwargs["datasource"] == "live"
    # ``live_trading`` retired in R24 — push no longer forwards it.
    assert "live_trading" not in kwargs or kwargs["live_trading"] is None
    assert kwargs["counters"] == {
        "delivered": 1,
        "content_error": 0,
        "fallback_scanned": 0,
    }


@pytest.mark.asyncio
async def test_once_heartbeat_write_failure_is_swallowed(
    _patched_scope: Any,
) -> None:
    """A raising ``write_heartbeat`` must NOT abort ``--once``: it still
    drains, stamps, and returns the processed count (fail-soft)."""
    ev = _FakeEvent(1, {"data": {}, "push": None})
    session = _FakeSession({1: ev}, [])
    sender = _hb_sender()

    def _boom(*_a: Any) -> None:
        raise RuntimeError("heartbeat backend exploded")

    with (
        _fake_health(_boom),
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
    ):
        processed = await sender.run_once()

    assert processed == 1  # drain unaffected
    assert ev.pushed_at is not None  # event still stamped


@pytest.mark.asyncio
async def test_once_exits_zero_db_down_even_if_heartbeat_raises() -> None:
    """Combined hostile case: DB unreachable AND the heartbeat write raises.
    ``--once`` must STILL return 0 (CI-smoke contract preserved end to end)."""
    sender = _hb_sender()

    @asynccontextmanager
    async def _boom_db() -> Any:
        raise OSError("Connect call failed ('127.0.0.1', 5432)")
        yield  # pragma: no cover

    def _boom_hb(*_a: Any) -> None:
        raise RuntimeError("heartbeat down too")

    with (
        _fake_health(_boom_hb),
        patch("polyecho.push.sender.session_scope", _boom_db),
    ):
        processed = await sender.run_once()

    assert processed == 0


@pytest.mark.asyncio
async def test_once_exits_zero_db_down_when_health_module_absent() -> None:
    """If ``polyecho.shared.health`` is genuinely ABSENT (isolated build),
    the lazy import inside ``_emit_heartbeat`` raises ImportError — which is
    swallowed — so ``--once`` still exits 0 with the DB down. (Proves the
    lazy-import seam, not just a patched double.)"""
    import sys

    sender = _hb_sender()

    @asynccontextmanager
    async def _boom_db() -> Any:
        raise OSError("Connect call failed ('127.0.0.1', 5432)")
        yield  # pragma: no cover

    prev = sys.modules.pop("polyecho.shared.health", None)
    try:
        with patch("polyecho.push.sender.session_scope", _boom_db):
            processed = await sender.run_once()
    finally:
        if prev is not None:
            sys.modules["polyecho.shared.health"] = prev

    assert processed == 0


@pytest.mark.asyncio
async def test_fallback_scan_writes_heartbeat_with_scanned_count(
    _patched_scope: Any,
) -> None:
    """The safety-net scan reports ``fallback_scanned`` = events it processed
    and a real delivery also bumps ``delivered``."""
    ev = _FakeEvent(21, {"data": {}, "push": {"title": "t", "body": "b"}})
    sub = _FakeSub("https://push.example/stranded")
    session = _FakeSession({21: ev}, [sub])
    sender = _hb_sender()

    calls: list[tuple[Any, ...]] = []

    with (
        _fake_health(lambda *a, **kw: calls.append((a, kw))),
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush", return_value=None),
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
    ):
        processed = await sender.run_fallback_scan()

    assert processed == 1
    assert len(calls) == 1
    _args, kw = calls[0]
    assert _args == ("push",)  # only ``kind`` is positional (F0 kw-only)
    assert kw["phase"] == "idle"
    assert kw["counters"]["fallback_scanned"] == 1
    assert kw["counters"]["delivered"] == 1


@pytest.mark.asyncio
async def test_fallback_scan_heartbeat_failure_does_not_crash_scheduler(
    _patched_scope: Any,
) -> None:
    """A raising heartbeat inside the scheduler-driven fallback scan must be
    swallowed (the 'safety net must not crash the supervised process'
    contract holds even when observability fails)."""
    pending = _FakeEvent(32, {"data": {}, "push": None})
    session = _FakeSession({32: pending}, [])
    sender = _hb_sender()

    def _boom(*_a: Any) -> None:
        raise RuntimeError("hb backend down")

    with (
        _fake_health(_boom),
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
    ):
        processed = await sender.run_fallback_scan()

    assert processed == 1  # scan completed; heartbeat error did not surface


@pytest.mark.asyncio
async def test_fallback_scan_db_down_still_emits_heartbeat_and_returns_zero(
    _patched_scope: Any,
) -> None:
    """DB-down fallback scan: returns 0 (unchanged) AND still emits a
    fail-soft heartbeat with ``fallback_scanned`` = 0."""
    sender = _hb_sender()
    calls: list[tuple[Any, ...]] = []

    @asynccontextmanager
    async def _boom_db() -> Any:
        raise OSError("Connect call failed ('127.0.0.1', 5432)")
        yield  # pragma: no cover

    with (
        _fake_health(lambda *a, **kw: calls.append((a, kw))),
        patch("polyecho.push.sender.session_scope", _boom_db),
    ):
        processed = await sender.run_fallback_scan()

    assert processed == 0
    assert len(calls) == 1
    assert calls[0][1]["counters"]["fallback_scanned"] == 0


@pytest.mark.asyncio
async def test_all_content_error_heartbeat_counts_content_error(
    _patched_scope: Any,
) -> None:
    """An all-413 cycle bumps ``content_error`` (not ``delivered``) — the
    counters mirror the existing terminal classification."""
    ev = _FakeEvent(11, {"data": {}, "push": {"title": "t", "body": "b" * 9}})
    subs = [_FakeSub("https://push.example/big")]
    session = _FakeSession({11: ev}, subs)
    sender = _hb_sender()

    calls: list[tuple[Any, ...]] = []

    with (
        _fake_health(lambda *a, **kw: calls.append((a, kw))),
        patch("polyecho.push.sender.session_scope", _patched_scope(session)),
        patch("polyecho.push.sender.webpush", side_effect=_webpush_exc(413)),
        patch(
            "polyecho.push.sender.build_vapid_claims",
            return_value={"sub": "x"},
        ),
    ):
        await sender.run_once()

    assert len(calls) == 1
    counters = calls[0][1]["counters"]
    assert counters["content_error"] == 1
    assert counters["delivered"] == 0


@pytest.mark.asyncio
async def test_emit_heartbeat_swallows_all_but_propagates_cancelled() -> None:
    """``_emit_heartbeat`` swallows every error EXCEPT
    ``asyncio.CancelledError`` (which must propagate so graceful shutdown is
    never silently eaten)."""
    from polyecho.push.sender import _HeartbeatCounters

    sender = _hb_sender()

    # Arbitrary backend failure → swallowed (no raise).
    with _fake_health(MagicMock(side_effect=ValueError("nope"))):
        await sender._emit_heartbeat("idle", _HeartbeatCounters())

    # Cancellation → must propagate.
    def _cancel(*_a: Any, **_kw: Any) -> None:
        raise asyncio.CancelledError

    with _fake_health(_cancel):
        with pytest.raises(asyncio.CancelledError):
            await sender._emit_heartbeat("idle", _HeartbeatCounters())


@pytest.mark.asyncio
async def test_emit_heartbeat_awaits_coroutine_write_impl() -> None:
    """The frozen ``write_heartbeat`` may be sync OR async; an async impl is
    awaited (no 'coroutine was never awaited' leak)."""
    from polyecho.push.sender import _HeartbeatCounters

    sender = _hb_sender()
    seen: list[str] = []

    async def _async_wh(*args: Any, **_kw: Any) -> None:
        seen.append(args[0])

    with _fake_health(_async_wh):
        await sender._emit_heartbeat("sending", _HeartbeatCounters())

    assert seen == ["push"]


@pytest.mark.asyncio
async def test_supervised_loop_emits_heartbeat_and_survives_hb_error() -> None:
    """The supervised stream emits a per-iteration heartbeat; a raising
    heartbeat must NOT break the reconnect/drain loop (it still reaches the
    NOTIFY wait and is then cleanly cancelled)."""
    sender = _hb_sender()
    phases: list[str] = []

    def _wh(*_a: Any, **kw: Any) -> None:
        phases.append(kw["phase"])  # phase is keyword-only in F0's contract
        raise RuntimeError("hb always fails")  # fail-soft must absorb this

    @asynccontextmanager
    async def _fake_listen(_channel: str) -> Any:
        async def _gen() -> Any:
            # Reaching here proves the (failing) heartbeats before the
            # NOTIFY wait did not abort the loop; now stop the test.
            raise asyncio.CancelledError
            yield  # pragma: no cover

        gen = _gen()
        try:
            yield gen
        finally:
            with __import__("contextlib").suppress(Exception):
                await gen.aclose()

    async def _fake_backfill(
        counters: Any = None,
    ) -> int:  # noqa: ARG001
        return 0

    with (
        _fake_health(_wh),
        patch("polyecho.push.sender.pg_listen_iter", _fake_listen),
        patch.object(sender, "run_backfill", _fake_backfill),
    ):
        with pytest.raises(asyncio.CancelledError):
            await sender.run()

    # Both the sending and idle per-iteration heartbeats were attempted
    # (and their RuntimeErrors swallowed — the loop reached the NOTIFY wait).
    assert "sending" in phases
    assert "idle" in phases
