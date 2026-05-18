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

Security (P0-1 — operator pre-provisioned secret)
-------------------------------------------------
* Confirmation verifies the caller passphrase against the **operator
  pre-provisioned** danger-passphrase hash
  (:func:`polyecho.shared.danger_passphrase.get_danger_passphrase_hash`).
  ``confirm_pending_action`` **never** writes/derives the hash from caller
  input — if the hash is unset it **fails closed**
  (:attr:`PendingActionError.Reason.NOT_PROVISIONED`), never auto-provisions.
  Only the CLI (:func:`~polyecho.shared.danger_passphrase.set_danger_passphrase`)
  may provision it; no web / DB-write path can self-provision.
* ``create_pending_action`` stores **no** passphrase material at all (the
  per-row ``confirm_passphrase_hash`` column is left ``NULL``) — the
  secret of record is the single operator hash, not anything the caller
  supplied.
* Verification is constant-time
  (:func:`polyecho.shared.danger_passphrase.verify_danger_passphrase` →
  :func:`hmac.compare_digest`).
* A server-enforced **minimum confirm age** (:data:`MIN_CONFIRM_AGE_SECONDS`)
  derived from the row's ``created_at`` makes the Dashboard "5s countdown"
  authoritative server-side (a too-fast confirm raises ``TOO_SOON``), and
  ``expires_at`` still bounds the window (``EXPIRED``).
* A wrong passphrase **raises** and does NOT consume / expire the row
  (TTL alone resolves it) — and nothing sensitive is logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from polyecho.shared.danger_passphrase import (
    get_danger_passphrase_hash,
    verify_danger_passphrase,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MIN_CONFIRM_AGE_SECONDS",
    "PendingActionError",
    "PendingActionStatus",
    "PendingActionType",
    "confirm_pending_action",
    "create_pending_action",
    "expire_stale",
]

#: Server-enforced minimum age (seconds) between row creation and a valid
#: confirm — the authoritative form of the PRD §5.7 Dashboard "5-second
#: countdown".  A confirm arriving before ``created_at + this`` is rejected
#: with :attr:`PendingActionError.Reason.TOO_SOON` regardless of any
#: client-side timer.  FROZEN — the web mirror surfaces the same countdown.
MIN_CONFIRM_AGE_SECONDS = 5

#: Default confirmation-window TTL (seconds); also the server upper bound.
#: Exposed so the web mirror shows a matching expiry.  (PRD §5.7.)
DEFAULT_TTL_SECONDS = 300


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
        #: The operator danger passphrase has not been provisioned (CLI).
        #: Fail-closed: confirm refuses rather than auto-provisioning (P0-1).
        NOT_PROVISIONED = "not_provisioned"
        #: Confirmed before ``created_at + MIN_CONFIRM_AGE_SECONDS`` — the
        #: server-enforced "5s countdown" (PRD §5.7).
        TOO_SOON = "too_soon"

    def __init__(self, reason: PendingActionError.Reason) -> None:
        self.reason = reason
        super().__init__(reason.value)


async def create_pending_action(
    *,
    action_type: PendingActionType,
    params: dict[str, Any],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    session: Any = None,
) -> str:
    """Create one ``awaiting_confirm`` pending-action row; return its id.

    P0-1: the row stores **no** passphrase material — the per-row
    ``confirm_passphrase_hash`` column is intentionally left ``NULL``.
    Authorisation is the **operator pre-provisioned** danger-passphrase
    hash (verified by :func:`confirm_pending_action`); a caller can no
    longer "set the secret" by being the one who created the row.  Only
    ``expires_at = created_at + ttl_seconds`` is recorded here; the
    server-enforced minimum age uses the DB ``created_at``.

    .. note:: **FROZEN signature change (P0-1)** — the old ``passphrase``
       keyword has been **removed**.  Storing a caller-supplied hash *was*
       the vulnerability (first-confirm self-set = no real second factor);
       there is nothing safe to do with a passphrase at create time, so the
       parameter is gone.  Wave-2 (web create action) calls this without a
       passphrase; the secret is checked only at confirm.

    Args:
        action_type: One of :class:`PendingActionType`.
        params:      Opaque execution params (jsonb; e.g. market/size).
        ttl_seconds: Confirmation window (default
                     :data:`DEFAULT_TTL_SECONDS` = 300 s, PRD §5.7).
        session:     Optional caller session; ``None`` → new
                     :func:`~polyecho.shared.db.session_scope`.

    Returns:
        The new ``pending_actions.id`` as a ``str`` (UUID).
    """
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import PendingAction

    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    async def _create(sess: Any) -> str:
        row = PendingAction(
            action_type=str(action_type),
            params=params,
            status=str(PendingActionStatus.AWAITING_CONFIRM),
            # P0-1: never store caller-supplied passphrase material.
            confirm_passphrase_hash=None,
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
    :class:`~PendingActionError.Reason`:

    * ``NOT_FOUND``        — bad UUID / no such row.
    * ``ALREADY_RESOLVED`` — not in ``awaiting_confirm``.
    * ``EXPIRED``          — now ≥ ``expires_at``.
    * ``TOO_SOON``         — now < ``created_at + MIN_CONFIRM_AGE_SECONDS``
      (the server-enforced "5s countdown"; client timers are advisory).
    * ``NOT_PROVISIONED``  — the operator danger passphrase is unset.
      **Fail-closed**: confirm refuses; it never auto-provisions, and never
      writes/derives the hash from *passphrase* (P0-1).
    * ``BAD_PASSPHRASE``   — *passphrase* does not match the operator
      pre-provisioned hash (constant-time check).

    Verification is against
    :func:`polyecho.shared.danger_passphrase.get_danger_passphrase_hash`
    — the single operator secret — **not** anything stored on the row
    (``confirm_passphrase_hash`` is always ``NULL``).  A wrong passphrase
    (or too-soon / not-provisioned) does **not** consume / expire the row;
    only the TTL retires it (:func:`expire_stale`).  On success the row
    flips to ``confirmed`` and the action type is returned so the caller
    (core, live-locked) can execute it.  Nothing sensitive is logged.

    Args:
        action_id:  The ``pending_actions.id`` (UUID string).
        passphrase: The operator danger passphrase to verify.
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

        now = datetime.now(UTC)

        expires_at = row.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if now >= expires_at:
                raise PendingActionError(
                    PendingActionError.Reason.EXPIRED
                )

        # Server-enforced minimum age (the authoritative "5s countdown").
        # ``created_at`` is a NOT NULL server-default column; treat a naive
        # value as UTC (matches the expires_at handling above).  R3
        # FAIL-CLOSED: if ``created_at`` is somehow NULL we cannot prove the
        # 5s gate elapsed, so we must REFUSE (TOO_SOON) — never silently
        # allow an un-gated fast-confirm of a danger-tier action.
        created_at = row.created_at
        if created_at is None:
            logger.warning(
                "pending_action_missing_created_at_fail_closed",
                extra={"action_id": action_id},
            )
            raise PendingActionError(PendingActionError.Reason.TOO_SOON)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if now < created_at + timedelta(seconds=MIN_CONFIRM_AGE_SECONDS):
            raise PendingActionError(PendingActionError.Reason.TOO_SOON)

        # P0-1 fail-closed: the secret of record is the operator
        # pre-provisioned hash.  If it is unset, refuse — NEVER
        # auto-provision and NEVER derive it from `passphrase`.
        stored = await get_danger_passphrase_hash(sess)
        if stored is None:
            logger.warning(
                "pending_action_not_provisioned",
                extra={"action_id": action_id},
            )
            raise PendingActionError(
                PendingActionError.Reason.NOT_PROVISIONED
            )

        if not verify_danger_passphrase(passphrase, stored):
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

    Expires an ``awaiting_confirm`` row when **either**:

    * ``expires_at`` is set and ``expires_at < now`` (normal TTL), **or**
    * ``expires_at IS NULL`` *and* ``created_at < now - DEFAULT_TTL`` — R3
      fix: a row with no ``expires_at`` was previously skipped forever and
      stayed permanently stuck in ``awaiting_confirm``.  A NULL-expires row
      whose age exceeds :data:`DEFAULT_TTL_SECONDS` is now expired so it
      cannot linger indefinitely.  (A NULL-expires row with NULL
      ``created_at`` cannot have its age proven, so it is left for manual
      remediation rather than being silently kept alive — confirm itself
      fail-closes on such a row via ``TOO_SOON``.)

    Idempotent; safe to run on a scheduler.

    Args:
        session: Optional caller session; ``None`` → new scope.

    Returns:
        The number of rows transitioned to ``expired``.
    """
    from sqlalchemy import and_, or_, update

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import PendingAction

    now = datetime.now(UTC)
    default_ttl_cutoff = now - timedelta(seconds=DEFAULT_TTL_SECONDS)

    async def _expire(sess: Any) -> int:
        result = await sess.execute(
            update(PendingAction)
            .where(
                PendingAction.status
                == str(PendingActionStatus.AWAITING_CONFIRM),
                or_(
                    and_(
                        PendingAction.expires_at.is_not(None),
                        PendingAction.expires_at < now,
                    ),
                    and_(
                        PendingAction.expires_at.is_(None),
                        PendingAction.created_at.is_not(None),
                        PendingAction.created_at < default_ttl_cutoff,
                    ),
                ),
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
