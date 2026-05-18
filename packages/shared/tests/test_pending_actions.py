"""
tests/test_pending_actions.py
=============================
Phase-5 Wave-1 — :mod:`polyecho.shared.pending_actions` FROZEN contract
(P0-1: operator pre-provisioned danger passphrase).

No Postgres: an in-memory session double stands in for the row store **and**
the ``settings`` row (so ``get_danger_passphrase_hash`` resolves through the
same fake).  We exercise the full create→confirm lifecycle, the typed-error
reasons (incl. ``NOT_PROVISIONED`` / ``TOO_SOON``), the constant-time check,
fail-closed when the operator secret is unset, the server-enforced minimum
confirm age, and ``expire_stale``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from polyecho.shared.danger_passphrase import (
    DANGER_PASSPHRASE_SETTINGS_KEY,
    hash_danger_passphrase,
)
from polyecho.shared.pending_actions import (
    MIN_CONFIRM_AGE_SECONDS,
    PendingActionError,
    PendingActionStatus,
    PendingActionType,
    confirm_pending_action,
    create_pending_action,
    expire_stale,
)

from .conftest import run

# A creation time safely older than the server-enforced minimum confirm age
# so confirms in the happy-path tests are not rejected as TOO_SOON.
_OLD = datetime.now(UTC) - timedelta(seconds=MIN_CONFIRM_AGE_SECONDS + 60)


class _Row:
    """Stand-in for the PendingAction ORM row."""

    def __init__(self, **kw: Any) -> None:
        self.id = uuid.uuid4()
        self.action_type = kw["action_type"]
        self.params = kw.get("params")
        self.status = kw["status"]
        self.confirm_passphrase_hash = kw.get("confirm_passphrase_hash")
        self.expires_at = kw.get("expires_at")
        # Server-default in the real model; the double backdates it so the
        # MIN_CONFIRM_AGE_SECONDS gate does not reject happy-path confirms.
        self.created_at = kw.get("created_at", _OLD)


class _SettingRow:
    """Stand-in for the settings ORM row (danger_passphrase_hash)."""

    def __init__(self, key: str, value: Any) -> None:
        self.key = key
        self.value = value


class _Sess:
    """Tiny async session: ``add``/``flush`` insert; ``execute`` selects.

    Holds both pending-action rows and a single settings row so
    ``get_danger_passphrase_hash(sess)`` resolves against the same double.
    """

    def __init__(self, *, provisioned: str | None = None) -> None:
        self.rows: list[_Row] = []
        self.settings: dict[str, _SettingRow] = {}
        if provisioned is not None:
            self.settings[DANGER_PASSPHRASE_SETTINGS_KEY] = _SettingRow(
                DANGER_PASSPHRASE_SETTINGS_KEY,
                hash_danger_passphrase(provisioned),
            )

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        text = str(stmt).lower().lstrip()

        # NB: a SELECT on `settings` contains the column "updated_at" — match
        # the *statement verb* (leading token), not a substring, so the
        # settings lookup is never mis-routed to the bulk-UPDATE branch.
        if text.startswith("update"):
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
        key = next(iter(target.values()))

        class _R:
            def __init__(self, row: Any) -> None:
                self._row = row

            def scalar_one_or_none(self) -> Any:
                return self._row

        # settings lookup (key is a str) vs pending_actions (key is a UUID).
        if "settings" in text:
            return _R(self.settings.get(key))
        match = next((r for r in self.rows if r.id == key), None)
        return _R(match)


def _patch_model(sess: _Sess) -> Any:
    """Patch ``PendingAction`` so ``add`` receives our ``_Row``."""
    return patch(
        "polyecho.shared.models.PendingAction",
        side_effect=lambda **kw: _Row(**kw),
    )


def _new_action(sess: _Sess, **over: Any) -> str:
    kw: dict[str, Any] = {
        "action_type": PendingActionType.MANUAL_SELL,
        "params": {},
        "session": sess,
    }
    kw.update(over)
    with _patch_model(sess):
        action_id: str = run(create_pending_action(**kw))
    return action_id


# ---------------------------------------------------------------------------
# enums / reasons
# ---------------------------------------------------------------------------
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
        "not_provisioned",
        "too_soon",
    }


# ---------------------------------------------------------------------------
# create — stores NO caller passphrase material (P0-1)
# ---------------------------------------------------------------------------
def test_create_stores_no_passphrase_material() -> None:
    sess = _Sess()
    aid = _new_action(sess, params={"market": "0xabc", "shares": "3"})
    row = sess.rows[0]
    assert str(row.id) == aid
    assert row.status == "awaiting_confirm"
    # The per-row hash column is intentionally NULL — the secret of record
    # is the operator pre-provisioned hash, not anything the caller sent.
    assert row.confirm_passphrase_hash is None


def test_create_signature_has_no_passphrase_kw() -> None:
    import inspect

    sig = inspect.signature(create_pending_action)
    assert "passphrase" not in sig.parameters


# ---------------------------------------------------------------------------
# confirm — verifies the operator pre-provisioned secret
# ---------------------------------------------------------------------------
def test_confirm_happy_path_with_provisioned_secret() -> None:
    sess = _Sess(provisioned="operator-secret")
    aid = _new_action(sess, action_type=PendingActionType.MANUAL_SELL)
    result = run(
        confirm_pending_action(
            action_id=aid, passphrase="operator-secret", session=sess
        )
    )
    assert result is PendingActionType.MANUAL_SELL
    assert sess.rows[0].status == "confirmed"


def test_confirm_fails_closed_when_not_provisioned() -> None:
    """No operator secret → refuse; NEVER auto-provision from caller input."""
    sess = _Sess()  # nothing provisioned
    aid = _new_action(sess)
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="anything", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.NOT_PROVISIONED
    # Row untouched (not consumed / not expired) and still NO stored hash:
    # confirm must never write/derive the secret from caller input.
    assert sess.rows[0].status == "awaiting_confirm"
    assert sess.rows[0].confirm_passphrase_hash is None
    assert DANGER_PASSPHRASE_SETTINGS_KEY not in sess.settings


def test_wrong_passphrase_raises_and_does_not_consume() -> None:
    sess = _Sess(provisioned="correct")
    aid = _new_action(
        sess, action_type=PendingActionType.EMERGENCY_LIQUIDATE
    )
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="WRONG", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.BAD_PASSPHRASE
    assert sess.rows[0].status == "awaiting_confirm"


def test_confirm_too_soon_is_server_enforced() -> None:
    """A confirm before created_at + MIN_CONFIRM_AGE_SECONDS is rejected."""
    sess = _Sess(provisioned="pw")
    aid = _new_action(sess)
    # Force creation to "just now" — inside the minimum-age window.
    sess.rows[0].created_at = datetime.now(UTC)
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="pw", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.TOO_SOON
    assert sess.rows[0].status == "awaiting_confirm"


def test_expired_row_raises_expired() -> None:
    sess = _Sess(provisioned="pw")
    aid = _new_action(sess)
    sess.rows[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=aid, passphrase="pw", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.EXPIRED


def test_missing_row_raises_not_found() -> None:
    sess = _Sess(provisioned="pw")
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id=str(uuid.uuid4()), passphrase="x", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.NOT_FOUND


def test_bad_uuid_raises_not_found() -> None:
    sess = _Sess(provisioned="pw")
    with pytest.raises(PendingActionError) as ei:
        run(
            confirm_pending_action(
                action_id="not-a-uuid", passphrase="x", session=sess
            )
        )
    assert ei.value.reason is PendingActionError.Reason.NOT_FOUND


def test_already_resolved_raises() -> None:
    sess = _Sess(provisioned="pw")
    aid = _new_action(sess)
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
    sess = _Sess(provisioned="pw")
    aid = _new_action(sess)
    with patch(
        "polyecho.shared.danger_passphrase.hmac.compare_digest",
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
    _new_action(sess)
    _new_action(sess)
    sess.rows[0].expires_at = datetime.now(UTC) - timedelta(seconds=10)
    sess.rows[1].expires_at = datetime.now(UTC) + timedelta(seconds=300)
    n = run(expire_stale(session=sess))
    assert n == 1
    assert sess.rows[0].status == "expired"
    assert sess.rows[1].status == "awaiting_confirm"
