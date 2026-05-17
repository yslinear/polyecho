"""Web-push-sender: polls events table and delivers VAPID Web Push.

PRD reference: §5.8 (Web Push alert centre).

Design principles (PRD §5.8):
- Uses browser-native Web Push API + VAPID — zero third-party service dependency.
- Polls the ``events`` table; for each unpushed row, delivers to all rows in
  ``push_subscriptions`` via pywebpush.
- VAPID private/public keys are read from settings (never hardcoded).
- Event types are defined in PRD §5.8 and reproduced as constants below.

Event types (PRD §5.8):
    new_trade            — new copy-trade executed
    position_closed      — position closed / stop-loss / target hit
    daily_pnl_summary    — nightly PnL digest (23:00 Asia/Taipei)
    risk_triggered       — daily loss limit hit or strategy drift detected
    system_error         — API failure, bot restart, insufficient balance
    new_leader_candidate — high-score candidate added to pool
    pending_action       — dangerous operation awaiting confirmation
"""

from __future__ import annotations

import logging
from typing import Any

from polyecho.shared.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event type constants (PRD §5.8)
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


# ---------------------------------------------------------------------------
# WebPushSender
# ---------------------------------------------------------------------------


class WebPushSender:
    """Polls the ``events`` table and fans out Web Push notifications.

    PRD §5.8.

    The sender runs as an independent process (``web-push-sender``) that:
    1. Queries ``events`` where ``pushed_at IS NULL`` (unpushed events).
    2. For each event, sends a push message to every active subscription in
       ``push_subscriptions`` via VAPID-authenticated Web Push.
    3. Updates ``events.pushed_at = now()`` on success.
    4. Handles expired/invalid subscriptions by removing them from the DB.

    VAPID keys are loaded from ``get_settings().vapid_private_key`` and
    ``get_settings().vapid_public_key``.  The claim email is
    ``get_settings().vapid_claim_email``.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the web-push-sender polling loop.

        PRD §5.8 — polls at a short interval (e.g. 5s) and delivers any
        pending events.  Accumulates events while the receiver is offline
        and delivers them when they reconnect.

        Raises:
            NotImplementedError: until Phase 5.
        """
        raise NotImplementedError("Phase 5")

    async def push_event(self, event: dict[str, Any]) -> None:
        """Push a single event to all active subscriptions.

        PRD §5.8 — fan-out: one event → N subscriptions.

        Args:
            event: Row from the ``events`` table as a dict.  Must contain:
                   ``event_type`` (str), ``severity`` (str), ``payload`` (dict).

        Raises:
            NotImplementedError: until Phase 5.
        """
        raise NotImplementedError("Phase 5")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_pending_events(self) -> list[dict[str, Any]]:
        """Query the ``events`` table for rows where ``pushed_at IS NULL``.

        Returns:
            List of event dicts ordered by ``created_at`` ascending.

        Raises:
            NotImplementedError: until Phase 5.
        """
        raise NotImplementedError("Phase 5")

    async def _fetch_subscriptions(self) -> list[dict[str, Any]]:
        """Return all active push subscription rows from ``push_subscriptions``.

        Returns:
            List of subscription dicts with ``endpoint``, ``p256dh_key``,
            ``auth_key``, and ``id`` fields.

        Raises:
            NotImplementedError: until Phase 5.
        """
        raise NotImplementedError("Phase 5")

    def _send_to_subscription(
        self,
        subscription: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        """Deliver a single Web Push message via pywebpush + VAPID.

        Args:
            subscription: Subscription dict with ``endpoint``, ``p256dh_key``,
                          ``auth_key``.
            payload: JSON-serialisable push body (title, body, data, etc.).

        Returns:
            True on 201 Created, False on 410 Gone (subscription expired).

        Raises:
            NotImplementedError: until Phase 5.

        # TODO(verify): confirm pywebpush 2.x API (WebPusher constructor
        # signature changed between 1.x and 2.x).
        """
        raise NotImplementedError("Phase 5")

    async def _mark_pushed(self, event_id: int) -> None:
        """Set ``events.pushed_at = now()`` for the given event row.

        Args:
            event_id: PK of the delivered event (``events.id``).

        Raises:
            NotImplementedError: until Phase 5.
        """
        raise NotImplementedError("Phase 5")

    async def _remove_expired_subscription(self, subscription_id: str) -> None:
        """Delete a push subscription that returned HTTP 410 Gone.

        PRD §5.8 — expired subscriptions (device removed / permission revoked)
        must be cleaned up to avoid repeated failed delivery attempts.

        Args:
            subscription_id: UUID of the row in ``push_subscriptions``.

        Raises:
            NotImplementedError: until Phase 5.
        """
        raise NotImplementedError("Phase 5")
