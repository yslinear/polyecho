"""Trader-core: copy-trade execution engine.

PRD references: §4.2 (process isolation), §5.5 (real-time copy-trade executor).

Security contract (PRD §4.2, §7.2):
- This process is the ONLY process that holds private keys.
- It opens NO network ports.
- All control signals are received via PostgreSQL (settings table / LISTEN/NOTIFY).
- Logs must never contain private keys, API secrets, or order signatures (PRD §7.2).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from polyecho.shared.config import get_settings
from polyecho.shared.polymarket.client import PolymarketClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


class DecisionOutcome(StrEnum):
    """Result of the copy-trade evaluation."""

    EXECUTE = "execute"
    SKIP_LEADER_NOT_ACTIVE = "skip_leader_not_active"
    SKIP_DAILY_LOSS_LIMIT = "skip_daily_loss_limit"
    SKIP_MAX_CONCURRENT = "skip_max_concurrent"
    SKIP_CATEGORY_NOT_WHITELISTED = "skip_category_not_whitelisted"
    SKIP_STRATEGY_DRIFT = "skip_strategy_drift"
    SKIP_INSUFFICIENT_LIQUIDITY = "skip_insufficient_liquidity"
    SKIP_BELOW_MIN_SIZE = "skip_below_min_size"
    SKIP_CTF_EVENT_MISMATCH = "skip_ctf_event_mismatch"


@dataclass
class Decision:
    """Result of :meth:`TraderCore.evaluate_copy`.

    PRD §5.5 — evaluate_copy returns this object; caller decides whether to place
    the order based on ``outcome == DecisionOutcome.EXECUTE``.
    """

    outcome: DecisionOutcome
    copy_size_usd: float = 0.0
    reason: str = ""
    leader_address: str = ""
    market_id: str = ""
    side: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def should_execute(self) -> bool:
        return self.outcome == DecisionOutcome.EXECUTE


# ---------------------------------------------------------------------------
# Risk layer helpers (stubs)
# ---------------------------------------------------------------------------


async def _check_daily_loss(
    account_id: str,
    daily_loss_limit_usd: float,
) -> bool:
    """Return True if today's realised loss is still within the limit.

    PRD §5.5 risk layer: daily_loss < daily_loss_limit.

    Args:
        account_id: UUID of the trading account.
        daily_loss_limit_usd: Maximum allowed loss today (absolute USD value).

    Returns:
        True if copying is still allowed, False if limit breached.
    """
    raise NotImplementedError("Phase 4")


async def _check_max_concurrent(
    account_id: str,
    max_concurrent: int,
) -> bool:
    """Return True if open position count is below the cap.

    PRD §5.5 risk layer: concurrent positions < max_concurrent_positions.

    Args:
        account_id: UUID of the trading account.
        max_concurrent: Maximum allowed concurrent open positions.

    Returns:
        True if another position may be opened, False otherwise.
    """
    raise NotImplementedError("Phase 4")


async def _check_category_whitelist(
    market_category: str,
    category_whitelist: list[str] | None,
) -> bool:
    """Return True if the market category is allowed.

    PRD §5.5 risk layer: market category must be in category_whitelist (if set).

    Args:
        market_category: Category tag of the target market (e.g. "Politics").
        category_whitelist: Allowed categories; None means all are allowed.

    Returns:
        True if the category is permitted.
    """
    raise NotImplementedError("Phase 4")


async def _check_strategy_drift(leader_address: str) -> bool:
    """Return True if the leader shows NO strategy drift.

    PRD §5.3, §5.5 risk layer: if the leader's 7-day strategy type diverges from
    their 30-day type, this flag is set and copying is suspended.

    Args:
        leader_address: Wallet address of the leader.

    Returns:
        True if strategy is stable (no drift), False if drift detected.
    """
    raise NotImplementedError("Phase 4")


async def _check_ctf_event(
    market_id: str,
    condition_id: str,
) -> bool:
    """Cross-check that the CTF Exchange event is still live via Polygon RPC.

    PRD §5.5 — Polygon RPC CTF-event cross-check stub.  Queries the
    ConditionalTokens contract on Polygon to confirm the condition is not
    already resolved before placing the copy order.

    Args:
        market_id: Polymarket market identifier.
        condition_id: CTF condition ID (bytes32 hex string).

    Returns:
        True if the event is still open, False if already resolved.
    """
    # TODO(verify): confirm ConditionalTokens contract address and ABI on
    # Polygon PoS vs Polygon zkEVM with py-clob-client-v2 docs.
    raise NotImplementedError("Phase 4")


# ---------------------------------------------------------------------------
# WebSocket listener stub
# ---------------------------------------------------------------------------


class _UserChannelListener:
    """WebSocket subscriber for Polymarket User Channel.

    PRD §5.5 — listens for trade events on all active leader addresses and
    enqueues them for copy evaluation.

    Reference: https://docs.polymarket.com/api-reference/wss/user
    # TODO(verify): confirm User Channel authentication handshake with
    # py-clob-client-v2 WebSocket docs.
    """

    def __init__(self, leader_addresses: list[str], queue: asyncio.Queue[Any]) -> None:
        self._leaders = leader_addresses
        self._queue = queue

    async def run(self) -> None:
        """Connect and stream trade events; reconnect on disconnect.

        Raises:
            NotImplementedError: until Phase 4.
        """
        raise NotImplementedError("Phase 4")


# ---------------------------------------------------------------------------
# TraderCore
# ---------------------------------------------------------------------------


class TraderCore:
    """Main copy-trading execution service.

    PRD §4.2 / §5.5.

    Responsibilities:
    - Subscribe to Polymarket User Channel for all active leaders.
    - Cross-verify trade events via Polygon RPC CTF contract.
    - Evaluate each leader trade through multi-layer risk checks.
    - Place copy orders via PolymarketClient (FOK by default, IOC for
      emergency exits) using the account's private key.
    - Write results to ``my_trades`` table and emit events for web-push-sender.

    Security: this class is instantiated only within the ``trader-core`` Docker
    container / process.  It holds decrypted private key material in memory
    during its lifetime and must never log sensitive data.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: PolymarketClient | None = None
        self._trade_queue: asyncio.Queue[Any] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the trader-core service loop.

        Steps:
        1. Load active follow_relations from DB.
        2. Spin up _UserChannelListener for each active leader.
        3. Process trade events from queue via evaluate_copy → place order.
        4. Sleep-loop to reconcile open positions against on-chain state.

        Raises:
            NotImplementedError: until Phase 4.
        """
        raise NotImplementedError("Phase 4")

    async def evaluate_copy(self, leader_trade: Any) -> Decision:
        """Evaluate whether to copy a leader's trade.

        PRD §5.5 decision flow:
        1. Check leader is still in ``following`` status.
        2. Load the leader's copy_template from DB.
        3. Compute target copy size (ratio or fixed).
        4. Multi-layer risk checks (daily loss, max concurrent,
           category whitelist, strategy drift).
        5. CTF event cross-check via Polygon RPC.
        6. Check current orderbook liquidity.
        7. If all pass → return EXECUTE decision; caller places FOK order.

        Args:
            leader_trade: Row from ``leader_trades`` or parsed WebSocket event.

        Returns:
            Decision with outcome and copy parameters.

        Raises:
            NotImplementedError: until Phase 4.
        """
        raise NotImplementedError("Phase 4")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _place_fok_order(
        self,
        market_id: str,
        side: str,
        size: float,
        price: float,
    ) -> dict[str, Any]:
        """Place a Fill-Or-Kill limit order via PolymarketClient.

        PRD §5.5: FOK is the default order type to avoid partial fills.

        Args:
            market_id: Target market identifier.
            side: "BUY" or "SELL".
            size: Number of shares/contracts.
            price: Limit price (0–1 for binary markets).

        Returns:
            Raw order response dict from the CLOB API.

        Raises:
            NotImplementedError: until Phase 4.
        """
        raise NotImplementedError("Phase 4")

    async def _place_ioc_order(
        self,
        market_id: str,
        side: str,
        size: float,
        price: float,
    ) -> dict[str, Any]:
        """Place an Immediate-Or-Cancel order for emergency exits.

        PRD §5.5: IOC is used when speed of exit is more important than
        complete fill (e.g. daily loss limit just breached).

        Args:
            market_id: Target market identifier.
            side: "BUY" or "SELL".
            size: Number of shares/contracts.
            price: Limit price.

        Returns:
            Raw order response dict from the CLOB API.

        Raises:
            NotImplementedError: until Phase 4.
        """
        raise NotImplementedError("Phase 4")

    async def _emit_trade_event(
        self,
        trade_row: dict[str, Any],
        event_type: str,
    ) -> None:
        """Write an event row to the ``events`` table for web-push-sender.

        PRD §5.8 — trade completion and risk triggers are sent as events.

        Args:
            trade_row: Completed or rejected trade data.
            event_type: One of the event types defined in PRD §5.8.

        Raises:
            NotImplementedError: until Phase 4.
        """
        raise NotImplementedError("Phase 4")
