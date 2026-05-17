"""
polyecho.shared.pending_actions
===============================
**FROZEN Phase-5 Wave-1 contract module.**

PRD §5.7 danger-tier web-ops contract: high-risk operations (manual
sell, emergency liquidate) require a Dashboard countdown + passphrase
re-entry before ``trader-core`` (live-locked) executes them.  Wave-2
(web server actions create/confirm, core consumes the returned type)
imports the enums, :class:`PendingActionError` and the three coroutines
*verbatim*.

What lives here (and only here)
-------------------------------
* :class:`PendingActionType`    — ``manual_sell`` | ``emergency_liquidate``.
* :class:`PendingActionStatus`  — ``pending_actions.status`` value set.
* :class:`PendingActionError`   — typed failure (+ :class:`.Reason`).
* :func:`create_pending_action` — write an ``awaiting_confirm`` row.
* :func:`confirm_pending_action`— verify passphrase (constant-time) →
                                  ``confirmed``; returns the action type.
* :func:`expire_stale`          — bulk-expire overdue rows.

Security
--------
* The passphrase is **hashed** with the project's existing
  PBKDF2-HMAC-SHA256 primitive (same ``Settings.key_kdf_iterations`` the
  account-key crypto uses — ``polyecho.shared.crypto._derive_key``).  Only
  ``salt ‖ hash`` (base64) is persisted in ``confirm_passphrase_hash``;
  the plaintext is never stored or logged.
* Verification uses :func:`hmac.compare_digest` (constant-time).
* A wrong passphrase **raises** and does NOT consume / expire the row
  (TTL alone resolves it) — and nothing sensitive is logged.
"""

from __future__ import annotations

import base64
import hmac
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from polyecho.shared.config import get_settings
from polyecho.shared.crypto import _derive_key

logger = logging.getLogger(__name__)

__all__ = [
    "PendingActionError",
    "PendingActionStatus",
    "PendingActionType",
    "confirm_pending_action",
    "create_pending_action",
    "expire_stale",
]

#: PBKDF2 salt size (matches ``crypto._SALT_SIZE``; the passphrase hash is
#: self-describing as ``base64(salt ‖ 32-byte derived key)``).
_SALT_SIZE = 32


class PendingActionType(StrEnum):
    """PRD §5.7 danger-tier operations gated behind two-factor confirm."""

    MANUAL_SELL = "manual_sell"
    EMERGENCY_LIQUIDATE = "emergency_liquidate"


class PendingActionStatus(StrEnum):
    """``pending_actions.status`` value set (DB default ``awaiting_confirm``).

    Lifecycle: ``AWAITING_CONFIRM`` → ``CONFIRMED`` | ``EXPIRED`` |
    ``REJECTED``.
    """

    AWAITING_CONFIRM = "awaiting_confirm"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    REJECTED = "rejected"


class PendingActionError(Exception):
    """Typed failure raised by :func:`confirm_pending_action`.

    Carries a machine-readable :class:`Reason` so the caller (web action /
    core) can branch without string-matching.  The message never contains
    the passphrase or any derived secret.
    """

    class Reason(StrEnum):
        """Why a confirm attempt failed."""

        NOT_FOUND = "not_found"
        EXPIRED = "expired"
        ALREADY_RESOLVED = "already_resolved"
        BAD_PASSPHRASE = "bad_passphrase"

    def __init__(self, reason: PendingActionError.Reason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _hash_passphrase(passphrase: str) -> str:
    """Derive ``base64(salt ‖ PBKDF2-HMAC-SHA256(passphrase, salt))``.

    Reuses :func:`polyecho.shared.crypto._derive_key` (the same
    PBKDF2-HMAC-SHA256 + ``Settings.key_kdf_iterations`` primitive the
    account-key encryption uses) so the project has ONE KDF.  The salt is
    stored alongside the digest so verification is self-contained.
    """
    import os

    salt = os.urandom(_SALT_SIZE)
    iterations = get_settings().key_kdf_iterations
    digest = _derive_key(passphrase, salt, iterations)
    return base64.b64encode(salt + digest).decode()


def _verify_passphrase(passphrase: str, stored: str | None) -> bool:
    """Constant-time check of *passphrase* against a stored ``salt ‖ hash``.

    Returns ``False`` (never raises) on a missing / malformed stored value
    so the caller maps it to :attr:`PendingActionError.Reason.BAD_PASSPHRASE`
    uniformly.  Comparison uses :func:`hmac.compare_digest`.
    """
    if not stored:
        return False
    try:
        blob = base64.b64decode(stored)
    except (ValueError, TypeError):
        return False
    if len(blob) <= _SALT_SIZE:
        return False
    salt, expected = blob[:_SALT_SIZE], blob[_SALT_SIZE:]
    iterations = get_settings().key_kdf_iterations
    candidate = _derive_key(passphrase, salt, iterations)
    return hmac.compare_digest(candidate, expected)


async def create_pending_action(
    *,
    action_type: PendingActionType,
    params: dict[str, Any],
    passphrase: str,
    ttl_seconds: int = 300,
    session: Any = None,
) -> str:
    """Create one ``awaiting_confirm`` pending-action row; return its id.

    Stores only ``confirm_passphrase_hash`` (``base64(salt ‖ digest)``) —
    never the plaintext — and ``expires_at = now + ttl_seconds``.

    Args:
        action_type: One of :class:`PendingActionType`.
        params:      Opaque execution params (jsonb; e.g. market/size).
        passphrase:  The user passphrase authorising the action.
        ttl_seconds: Confirmation window (default 300 s = PRD §5.7).
        session:     Optional caller session; ``None`` → new
                     :func:`~polyecho.shared.db.session_scope`.

    Returns:
        The new ``pending_actions.id`` as a ``str`` (UUID).
    """
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import PendingAction

    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    pw_hash = _hash_passphrase(passphrase)

    async def _create(sess: Any) -> str:
        row = PendingAction(
            action_type=str(action_type),
            params=params,
            status=str(PendingActionStatus.AWAITING_CONFIRM),
            confirm_passphrase_hash=pw_hash,
            expires_at=expires_at,
        )
        sess.add(row)
        await sess.flush()
        action_id = str(row.id)
        logger.info(
            "pending_action_created",
            extra={
                "action_id": action_id,
                "action_type": str(action_type),
                "ttl_seconds": ttl_seconds,
            },
        )
        return action_id

    if session is not None:
        return await _create(session)
    async with session_scope() as owned:
        return await _create(owned)


async def confirm_pending_action(
    *,
    action_id: str,
    passphrase: str,
    session: Any = None,
) -> PendingActionType:
    """Confirm a pending action; return its :class:`PendingActionType`.

    Loads the row and raises :class:`PendingActionError` with the precise
    :class:`~PendingActionError.Reason` when it is missing
    (``NOT_FOUND``), past ``expires_at`` (``EXPIRED``), not
    ``awaiting_confirm`` (``ALREADY_RESOLVED``), or the passphrase fails
    the constant-time check (``BAD_PASSPHRASE``).

    A wrong passphrase does **not** consume / expire the row — the TTL is
    the only thing that retires it (and :func:`expire_stale` enforces it).
    On success the row flips to ``confirmed`` and the action type is
    returned so the caller (core, live-locked) can execute it.

    Args:
        action_id:  The ``pending_actions.id`` (UUID string).
        passphrase: The passphrase to verify.
        session:    Optional caller session; ``None`` → new scope.

    Returns:
        The confirmed :class:`PendingActionType`.

    Raises:
        PendingActionError: With ``.reason`` set as documented above.
    """
    import uuid

    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import PendingAction

    async def _confirm(sess: Any) -> PendingActionType:
        try:
            pk: Any = uuid.UUID(action_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise PendingActionError(
                PendingActionError.Reason.NOT_FOUND
            ) from exc

        row = (
            await sess.execute(
                select(PendingAction).where(PendingAction.id == pk)
            )
        ).scalar_one_or_none()
        if row is None:
            raise PendingActionError(PendingActionError.Reason.NOT_FOUND)

        if row.status != str(PendingActionStatus.AWAITING_CONFIRM):
            raise PendingActionError(
                PendingActionError.Reason.ALREADY_RESOLVED
            )

        expires_at = row.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expires_at:
                raise PendingActionError(
                    PendingActionError.Reason.EXPIRED
                )

        if not _verify_passphrase(passphrase, row.confirm_passphrase_hash):
            # Do NOT log the passphrase; do NOT consume/expire the row.
            logger.warning(
                "pending_action_bad_passphrase",
                extra={"action_id": action_id},
            )
            raise PendingActionError(
                PendingActionError.Reason.BAD_PASSPHRASE
            )

        row.status = str(PendingActionStatus.CONFIRMED)
        logger.info(
            "pending_action_confirmed",
            extra={"action_id": action_id, "action_type": row.action_type},
        )
        return PendingActionType(row.action_type)

    if session is not None:
        return await _confirm(session)
    async with session_scope() as owned:
        return await _confirm(owned)


async def expire_stale(session: Any = None) -> int:
    """Bulk-mark overdue ``awaiting_confirm`` rows as ``expired``.

    Updates every row whose ``status == 'awaiting_confirm'`` and
    ``expires_at < now``.  Idempotent; safe to run on a scheduler.

    Args:
        session: Optional caller session; ``None`` → new scope.

    Returns:
        The number of rows transitioned to ``expired``.
    """
    from sqlalchemy import update

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import PendingAction

    now = datetime.now(UTC)

    async def _expire(sess: Any) -> int:
        result = await sess.execute(
            update(PendingAction)
            .where(
                PendingAction.status
                == str(PendingActionStatus.AWAITING_CONFIRM),
                PendingAction.expires_at.is_not(None),
                PendingAction.expires_at < now,
            )
            .values(status=str(PendingActionStatus.EXPIRED))
        )
        count = int(result.rowcount or 0)
        if count:
            logger.info("pending_actions_expired", extra={"count": count})
        return count

    if session is not None:
        return await _expire(session)
    async with session_scope() as owned:
        return await _expire(owned)
