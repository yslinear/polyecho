"""
tests/test_pending_actions.py
=============================
Phase-5 Wave-1 — :mod:`polyecho.shared.pending_actions` binding contract.

No Postgres: an in-memory session double stands in for the row store so
we exercise the full create→confirm lifecycle, the typed-error reasons,
the constant-time passphrase check, and ``expire_stale``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from polyecho.shared.pending_actions import (
    PendingActionError,
    PendingActionStatus,
    PendingActionType,
    confirm_pending_action,
    create_pending_action,
    expire_stale,
)

from .conftest import run


class _Row:
    """Stand-in for the PendingAction ORM row."""

    def __init__(self, **kw: Any) -> None:
        self.id = uuid.uuid4()
        self.action_type = kw["action_type"]
        self.params = kw.get("params")
        self.status = kw["status"]
        self.confirm_passphrase_hash = kw.get("confirm_passphrase_hash")
        self.expires_at = kw.get("expires_at")


class _Sess:
    """Tiny async session: ``add``/``flush`` insert; ``execute`` selects."""

    def __init__(self) -> None:
        self.rows: list[_Row] = []

    def add(self, obj: Any) -> None:
        self.rows.append(obj)  # already a _Row built by the patched model

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        # select(PendingAction).where(id == pk) — find by the bound pk.
        text = str(stmt).lower()
        if "update" in text:
            now = datetime.now(UTC)
            n = 0
            for r in self.rows:
                ea = r.expires_at
                if ea is not None and ea.tzinfo is None:
                    ea = ea.replace(tzinfo=UTC)
                if (
                    r.status == PendingActionStatus.AWAITING_CONFIRM.value
                    and ea is not None
                    and ea < now
                ):
                    r.status = str(PendingActionStatus.EXPIRED)
                    n += 1

            class _U:
                rowcount = n

            return _U()

        target = stmt.compile().params
        pk = next(iter(target.values()))
        match = next((r for r in self.rows if r.id == pk), None)

        class _R:
            def __init__(self, row: Any) -> None:
                self._row = row

            def scalar_one_or_none(self) -> Any:
                return self._row

        return _R(match)


def _patch_model(sess: _Sess) -> Any:
    """Patch ``PendingAction`` so ``add`` receives our ``_Row``."""
    return patch(
        "polyecho.shared.models.PendingAction",
        side_effect=lambda **kw: _Row(**kw),
    )


def test_enums_match_db_values() -> None:
    assert PendingActionType.MANUAL_SELL.value == "manual_sell"
    assert PendingActionType.EMERGENCY_LIQUIDATE.value == "emergency_liquidate"
    assert PendingActionStatus.AWAITING_CONFIRM.value == "awaiting_confirm"
    assert {s.value for s in PendingActionStatus} == {
        "awaiting_confirm",
        "confirmed",
        "expired",
        "rejected",
    }
    assert {r.value for r in PendingActionError.Reason} == {
        "not_found",
        "expired",
        "already_resolved",
        "bad_passphrase",
    }


def test_create_then_confirm_happy_path() -> None:
    sess = _Sess()
    with _patch_model(sess):
        aid = run(
            create_pending_action(
                action_type=PendingActionType.MANUAL_SELL,
                params={"market": "0xabc", "shares": "3"},
                passphrase="hunter2",
                ttl_seconds=300,
                session=sess,
            )
        )
    row = sess.rows[0]
    assert str(row.id) == aid
    assert row.status == "awaiting_confirm"
    # Passphrase is hashed, never stored in plaintext.
    assert row.confirm_passphrase_hash
    assert "hunter2" not in row.confirm_passphrase_hash

    result = run(
        confirm_pending_action(
            action_id=aid, passphrase="hunter2", session=sess
        )
    )
    assert result is PendingActionType.MANUAL_SELL
    assert row.status == "confirmed"


def test_wrong_passphrase_raises_and_does_not_consume() -> None:
    sess = _Sess()
    with _patch_model(sess):
        aid = run(
            create_pending_action(
                action_type=PendingActionType.EMERGENCY_LIQUIDATE,
                params={},
                passphrase="correct",
                session=sess,
            )
        )
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="WRONG", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.BAD_PASSPHRASE
    # Row NOT consumed/expired on a single wrong try (TTL handles it).
    assert sess.rows[0].status == "awaiting_confirm"


def test_expired_row_raises_expired() -> None:
    sess = _Sess()
    with _patch_model(sess):
        aid = run(
            create_pending_action(
                action_type=PendingActionType.MANUAL_SELL,
                params={},
                passphrase="pw",
                ttl_seconds=300,
                session=sess,
            )
        )
    # Force it past its window.
    sess.rows[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="pw", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.EXPIRED


def test_missing_row_raises_not_found() -> None:
    sess = _Sess()
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=str(uuid.uuid4()), passphrase="x", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.NOT_FOUND


def test_bad_uuid_raises_not_found() -> None:
    sess = _Sess()
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id="not-a-uuid", passphrase="x", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.NOT_FOUND


def test_already_resolved_raises() -> None:
    sess = _Sess()
    with _patch_model(sess):
        aid = run(
            create_pending_action(
                action_type=PendingActionType.MANUAL_SELL,
                params={},
                passphrase="pw",
                session=sess,
            )
        )
    sess.rows[0].status = "confirmed"
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="pw", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.ALREADY_RESOLVED


def test_confirm_uses_constant_time_compare() -> None:
    """The verify path must route through ``hmac.compare_digest``."""
    sess = _Sess()
    with _patch_model(sess):
        aid = run(
            create_pending_action(
                action_type=PendingActionType.MANUAL_SELL,
                params={},
                passphrase="pw",
                session=sess,
            )
        )
    with patch(
        "polyecho.shared.pending_actions.hmac.compare_digest",
        return_value=True,
    ) as cmp_mock:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="anything", session=sess
            )
        )
    cmp_mock.assert_called_once()


def test_expire_stale_bulk_marks_overdue_rows() -> None:
    sess = _Sess()
    with _patch_model(sess):
        run(
            create_pending_action(
                action_type=PendingActionType.MANUAL_SELL,
                params={},
                passphrase="pw",
                ttl_seconds=300,
                session=sess,
            )
        )
        run(
            create_pending_action(
                action_type=PendingActionType.MANUAL_SELL,
                params={},
                passphrase="pw",
                ttl_seconds=300,
                session=sess,
            )
        )
    # One overdue, one still fresh.
    sess.rows[0].expires_at = datetime.now(UTC) - timedelta(seconds=10)
    n = run(expire_stale(session=sess))
    assert n == 1
    assert sess.rows[0].status == "expired"
    assert sess.rows[1].status == "awaiting_confirm"
