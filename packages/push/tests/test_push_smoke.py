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
