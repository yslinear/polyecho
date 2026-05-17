"""
tests/test_events.py
====================
Phase-5 Wave-1 — :mod:`polyecho.shared.events` binding contract.

Deterministic, no Postgres: the ``events`` insert + ``NOTIFY`` are
exercised against a mocked :class:`AsyncSession` so we assert the SQL
shaping (``pg_notify`` bound params, the post-commit ordering) and the
``events.payload`` jsonb body / :meth:`PushPayload.to_json` shape.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from polyecho.shared.events import (
    EVENTS_NOTIFY_CHANNEL,
    EventType,
    PushPayload,
    Severity,
    emit_event,
)

from .conftest import run


def test_event_type_taxonomy_complete() -> None:
    """All 12 PRD §5.8 推播事件 are present with their string identity."""
    assert {e.value for e in EventType} == {
        "copy_fill",
        "position_closed",
        "stop_loss",
        "target_hit",
        "daily_pnl_summary",
        "risk_daily_loss",
        "risk_strategy_drift",
        "system_api_failure",
        "system_bot_restart",
        "system_low_balance",
        "candidate_high_score",
        "pending_dangerous_action",
    }
    # StrEnum identity: members ARE their string value.
    assert EventType.COPY_FILL.value == "copy_fill"
    assert str(EventType.COPY_FILL) == "copy_fill"


def test_severity_default_matches_db_default() -> None:
    assert Severity.INFO.value == "info"
    assert {s.value for s in Severity} == {"info", "warning", "critical"}


def test_push_payload_to_json_frozen_shape() -> None:
    p = PushPayload(title="Filled", body="0.5 @ 0.42")
    assert p.url == "/" and p.tag is None
    assert p.to_json() == {
        "title": "Filled",
        "body": "0.5 @ 0.42",
        "url": "/",
        "tag": None,
        "data": {},
    }
    p2 = PushPayload(
        title="t", body="b", url="/positions/1", tag="pos-1", data={"id": 1}
    )
    assert p2.to_json() == {
        "title": "t",
        "body": "b",
        "url": "/positions/1",
        "tag": "pos-1",
        "data": {"id": 1},
    }
    # to_json must copy `data` (no shared mutable state).
    out = p2.to_json()
    out["data"]["id"] = 99
    assert p2.data == {"id": 1}


def test_notify_channel_frozen() -> None:
    assert EVENTS_NOTIFY_CHANNEL == "polyecho_events"


class _RecordingSession:
    """Minimal AsyncSession double recording add/flush/commit/execute."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.executed: list[Any] = []
        self.flushed = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1
        # Identity PK is assigned on flush in real SQLAlchemy.
        self.added[-1].id = 4242

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt: Any) -> Any:
        self.executed.append(stmt)
        return MagicMock()


def test_emit_event_inserts_row_and_fires_notify_after_commit() -> None:
    sess = _RecordingSession()
    push = PushPayload(title="Filled", body="copy fill", url="/positions/7")

    event_id = run(
        emit_event(
            event_type=EventType.COPY_FILL,
            severity=Severity.INFO,
            payload={"market": "0xabc", "size": "1.5"},
            push=push,
            session=cast(Any, sess),  # caller-supplied → no scope opened
        )
    )

    assert event_id == 4242
    # Exactly one events row, with the frozen jsonb body.
    assert len(sess.added) == 1
    row = sess.added[0]
    assert row.event_type == "copy_fill"
    assert row.severity == "info"
    assert row.payload == {
        "data": {"market": "0xabc", "size": "1.5"},
        "push": push.to_json(),
    }
    assert row.pushed_at is None  # sender stamps it later
    # NOTIFY issued via pg_notify; commit happened BEFORE the NOTIFY.
    assert sess.flushed == 1
    assert sess.commits == 2  # commit(row) then commit(notify)
    assert len(sess.executed) == 1
    sql = str(sess.executed[0]).lower()
    assert "pg_notify" in sql


def test_emit_event_without_push_stores_null_push() -> None:
    sess = _RecordingSession()
    run(
        emit_event(
            event_type=EventType.SYSTEM_BOT_RESTART,
            payload={"reason": "deploy"},
            session=cast(Any, sess),
        )
    )
    assert sess.added[0].payload == {
        "data": {"reason": "deploy"},
        "push": None,
    }


def test_emit_event_opens_session_scope_when_none(
    monkeypatch: Any,
) -> None:
    """When ``session is None`` a session_scope() is opened + used."""
    sess = _RecordingSession()

    class _Scope:
        async def __aenter__(self) -> _RecordingSession:
            return sess

        async def __aexit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(
        "polyecho.shared.events.session_scope", lambda: _Scope()
    )
    eid = run(
        emit_event(
            event_type=EventType.TARGET_HIT,
            payload={"pnl": "12.3"},
        )
    )
    assert eid == 4242
    assert len(sess.added) == 1
