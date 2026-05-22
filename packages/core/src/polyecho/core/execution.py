"""
polyecho.core.execution
========================
Order execution + FIFO sell-match (PolyHermes brief §3/§5, PRD §5.5).

Per-op operator confirmation lives at the web layer
(``dangerous-action`` + account-import passphrase).  This module
dispatches risk-PASSED COPY decisions directly to
:class:`~polyecho.shared.polymarket.client.PolymarketClient.place_order`
when a ``client_factory`` is configured; without a configured factory
the executor records the intended order as a skip (e.g. the fixture /
audit-only deployment with no signing keys wired in).

FIFO sell-match (brief §3): on a SELL, shares are matched against
unmatched BUY lots keyed ``(account, condition, outcome)`` oldest-first,
mutating ``MyTrade.matched_qty`` / ``remaining_qty``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from polyecho.shared.copytrading import (
    Decision,
    DecisionOutcome,
    EvaluatedCopy,
    SkipReason,
    TradeSide,
)
from polyecho.shared.polymarket.client import GeoRestrictionError, PolymarketClient

logger = logging.getLogger(__name__)

_ZERO = Decimal(0)


@dataclass
class BuyLot:
    """One unmatched BUY lot for FIFO sell-matching (brief §3/§5)."""

    trade_id: str
    account_id: uuid.UUID
    condition_id: str
    outcome: str
    size: Decimal
    price: Decimal
    matched_qty: Decimal = _ZERO
    opened_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def remaining_qty(self) -> Decimal:
        return self.size - self.matched_qty


@dataclass
class SellMatch:
    """Result of FIFO-matching a SELL against BUY lots (brief §3/§5)."""

    matched: list[tuple[str, Decimal]]  # (buy_trade_id, qty)
    unmatched_qty: Decimal
    realised_pnl_usd: Decimal


class FifoLedger:
    """In-proc FIFO BUY-lot ledger keyed ``(account, condition, outcome)``.

    The durable accounting lives in ``my_trades.matched_qty`` /
    ``remaining_qty``; this object is the pure, restart-rebuildable view
    the executor mutates and the engine persists.
    """

    def __init__(self) -> None:
        self._lots: dict[tuple[str, str, str], list[BuyLot]] = {}

    @staticmethod
    def _key(account_id: uuid.UUID, condition_id: str, outcome: str) -> tuple[str, str, str]:
        return (str(account_id), condition_id, outcome)

    def add_buy(self, lot: BuyLot) -> None:
        self._lots.setdefault(self._key(lot.account_id, lot.condition_id, lot.outcome), []).append(
            lot
        )

    def open_lots(self, account_id: uuid.UUID, condition_id: str, outcome: str) -> list[BuyLot]:
        return [
            lot
            for lot in self._lots.get(self._key(account_id, condition_id, outcome), [])
            if lot.remaining_qty > _ZERO
        ]

    def match_sell(
        self,
        *,
        account_id: uuid.UUID,
        condition_id: str,
        outcome: str,
        sell_size: Decimal,
        sell_price: Decimal,
        mutate: bool = True,
    ) -> SellMatch:
        """FIFO-match *sell_size* against oldest open BUY lots (brief §3).

        ``mutate`` controls whether ``BuyLot.matched_qty`` is advanced;
        callers pass ``False`` when computing a read-only preview.
        """
        remaining = sell_size
        matched: list[tuple[str, Decimal]] = []
        realised = _ZERO
        for lot in self._lots.get(self._key(account_id, condition_id, outcome), []):
            if remaining <= _ZERO:
                break
            avail = lot.remaining_qty
            if avail <= _ZERO:
                continue
            take = avail if avail < remaining else remaining
            if mutate:
                lot.matched_qty += take
            remaining -= take
            realised += (sell_price - lot.price) * take
            matched.append((lot.trade_id, take))
        return SellMatch(
            matched=matched,
            unmatched_qty=remaining,
            realised_pnl_usd=realised,
        )


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of one execution attempt.

    ``placed`` is ``True`` when the live SDK ``place_order`` returned a
    response; ``False`` indicates the decision was non-COPY or all retry
    attempts failed.
    """

    placed: bool
    decision: Decision
    skip_reason: SkipReason | None = None
    order_response: dict[str, Any] | None = None
    detail: str | None = None
    sell_match: SellMatch | None = None


class OrderExecutor:
    """Places orders on PASS via :class:`PolymarketClient`.

    Args:
        client_factory: ``(EvaluatedCopy) -> PolymarketClient`` invoked
                        before each placement.
        ledger:         The :class:`FifoLedger` for SELL matching.
        max_retries:    ≤ 2 re-sign retries on transient failure (brief §3).
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[EvaluatedCopy], Awaitable[PolymarketClient]] | None = None,
        ledger: FifoLedger | None = None,
        max_retries: int = 2,
    ) -> None:
        self._client_factory = client_factory
        self._ledger = ledger if ledger is not None else FifoLedger()
        self._max_retries = max(0, min(2, max_retries))

    @property
    def ledger(self) -> FifoLedger:
        return self._ledger

    async def execute(
        self,
        evaluated: EvaluatedCopy,
        *,
        size_override: Decimal | None = None,
    ) -> ExecutionResult:
        """Execute a risk-PASSED COPY decision.

        ``size_override`` is the risk-gate's ``adjusted_size`` (P1-1): when
        the :class:`~polyecho.core.risk.LayeredRiskGate` normalised/clamped
        the trade, the engine MUST place the gate-approved size, not the
        original (too-large) ``decision.size``.  When ``None`` the
        decision's own size is used unchanged.  The returned
        :class:`ExecutionResult` carries a ``decision`` rebuilt with the
        effective size so every downstream consumer (FIFO lot, trade
        record, push payload) sees the size that was actually placed.
        """
        decision = evaluated.decision
        if decision.outcome is not DecisionOutcome.COPY:
            return ExecutionResult(
                placed=False,
                decision=decision,
                skip_reason=decision.skip_reason,
                detail="non-COPY decision — nothing to execute",
            )

        # P1-1: the placed notional MUST equal the gate-approved size.
        if size_override is not None and size_override != decision.size:
            decision = replace(decision, size=size_override)
        size = decision.size or _ZERO
        price = decision.price or _ZERO
        outcome_name = str(
            evaluated.raw.get("outcome") or decision.token_id or evaluated.condition_id
        )

        # FIFO sell-match advances the durable BUY-lot ledger so every
        # placed SELL is reflected in realised-PnL accounting.  Audit-
        # only deployments (no ``client_factory``) compute the match as
        # READ-ONLY so a dry-run SELL never corrupts the live ledger.
        sell_match: SellMatch | None = None
        if decision.side is TradeSide.SELL:
            sell_match = self._ledger.match_sell(
                account_id=evaluated.account_id,
                condition_id=evaluated.condition_id,
                outcome=outcome_name,
                sell_size=size,
                sell_price=price,
                mutate=self._client_factory is not None,
            )

        if self._client_factory is None:
            # Audit-only path: log the intended order but place nothing.
            logger.info(
                "order_intent_audit_only",
                extra={
                    "leader": evaluated.leader_address,
                    "tx_hash": evaluated.tx_hash,
                    "account_id": str(evaluated.account_id),
                    "condition_id": evaluated.condition_id,
                    "side": decision.side.value if decision.side else None,
                    "size": str(size),
                    "price": str(price),
                    "order_type": decision.order_type,
                    "skip_reason": SkipReason.LIVE_LOCKED.value,
                },
            )
            return ExecutionResult(
                placed=False,
                decision=decision,
                skip_reason=SkipReason.LIVE_LOCKED,
                detail=(
                    "no client_factory configured — intended order "
                    "recorded for audit, no order placed"
                ),
                sell_match=sell_match,
            )
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._client_factory(evaluated)
                resp = await client.place_order(
                    token_id=str(decision.token_id),
                    side=decision.side.value if decision.side else "BUY",
                    price=float(price),
                    size=float(size),
                    order_type=decision.order_type,
                    neg_risk=bool(evaluated.raw.get("neg_risk", False)),
                    tick_size=str(evaluated.raw.get("tick_size", "0.01")),
                )
                return ExecutionResult(
                    placed=True,
                    decision=decision,
                    order_response=resp,
                    detail=f"order placed (attempt {attempt + 1})",
                    sell_match=sell_match,
                )
            except GeoRestrictionError:
                # C5: a geo-restriction rejection is NOT transient — a
                # re-sign retry against the same region-blocked CLOB will
                # never succeed.  Propagate it UNCAUGHT (skipping the
                # retry loop) so the trader-core can trip the documented
                # emergency-stop (global_paused) instead of churning.
                logger.warning(
                    "order_place_geo_restricted_halting",
                    extra={"attempt": attempt + 1},
                )
                raise
            except Exception as exc:  # noqa: BLE001 - retry ≤2, re-sign
                last_exc = exc
                logger.warning(
                    "order_place_retry",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
        return ExecutionResult(
            placed=False,
            decision=decision,
            skip_reason=SkipReason.DATA_UNAVAILABLE,
            detail=(f"order placement failed after {self._max_retries + 1} attempts: {last_exc}"),
            sell_match=sell_match,
        )
