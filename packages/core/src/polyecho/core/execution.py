"""
polyecho.core.execution
========================
Order execution + FIFO sell-match (PolyHermes brief §3/§5, PRD §5.5).

HARD invariant (the Phase-4 money lock):

    The engine NEVER places a real order unless ``is_live_enabled()`` is
    True.  Default = dry-run: the full :class:`Decision` is computed, the
    intended order is logged + recorded as a ``CopySkip`` /
    structured-log line with :attr:`SkipReason.LIVE_LOCKED`, and the live
    SDK is **not** called.

All real placement goes through
:class:`~polyecho.shared.polymarket.client.PolymarketClient.place_order`,
which itself calls ``assert_live_allowed`` first — that chokepoint is
never bypassed.  This module additionally short-circuits on
``is_live_enabled()`` so the default path performs **zero** placement
work (the unit tests assert this).

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
    is_live_enabled,
)
from polyecho.shared.polymarket.client import PolymarketClient

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
        self._lots.setdefault(
            self._key(lot.account_id, lot.condition_id, lot.outcome), []
        ).append(lot)

    def open_lots(
        self, account_id: uuid.UUID, condition_id: str, outcome: str
    ) -> list[BuyLot]:
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

        R8: ``mutate`` MUST be ``False`` in dry-run.  P1-3 only fixed the
        BUY-add path; ``match_sell`` was still mutating
        ``BuyLot.matched_qty`` even when the live lock was OFF, so a
        dry-run SELL silently consumed the live FIFO ledger (corrupting
        realised-PnL accounting the moment live trading was later
        enabled).  When ``mutate is False`` the SellMatch is computed
        READ-ONLY (the lots are never touched) purely for the dry-run log
        / preview; the durable ledger is advanced only on the live path.
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

    ``placed`` is ``True`` only when a live order was actually submitted;
    in the default dry-run it is ``False`` and ``skip_reason`` is
    :attr:`SkipReason.LIVE_LOCKED` (the live-lock proof the tests assert).
    """

    placed: bool
    decision: Decision
    skip_reason: SkipReason | None = None
    order_response: dict[str, Any] | None = None
    detail: str | None = None
    sell_match: SellMatch | None = None


class OrderExecutor:
    """Places orders on PASS — live-locked by default (brief HARD invariant).

    Args:
        client_factory: ``(EvaluatedCopy) -> PolymarketClient`` built only
                        when a live placement is about to happen (never in
                        dry-run, so no key material is touched by default).
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
        """Execute (or dry-run-log) a risk-PASSED COPY decision.

        The default (live lock OFF) path computes everything, logs the
        intended order with ``SkipReason.LIVE_LOCKED`` and returns
        ``placed=False`` **without** building a client or calling any SDK.

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

        # FIFO sell-match is computed in BOTH modes (the dry-run log shows
        # the would-be realised PnL), but R8: it MUST NOT consume the live
        # FIFO ledger when the live lock is OFF — a dry-run SELL that
        # mutated ``BuyLot.matched_qty`` corrupts the durable accounting
        # the instant live trading is later enabled.  ``mutate`` is gated
        # on ``is_live_enabled()`` exactly like the BUY-add path (P1-3),
        # so dry-run is fully read-only over the ledger.
        sell_match: SellMatch | None = None
        if decision.side is TradeSide.SELL:
            sell_match = self._ledger.match_sell(
                account_id=evaluated.account_id,
                condition_id=evaluated.condition_id,
                outcome=outcome_name,
                sell_size=size,
                sell_price=price,
                mutate=is_live_enabled(),
            )

        # --- THE HARD LIVE LOCK (default OFF ⇒ place nothing) ------------
        if not is_live_enabled():
            logger.info(
                "order_intent_dry_run",
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
                    "live trading is OFF (POLYECHO_LIVE_TRADING is not "
                    "'true') — intended order recorded for audit, no funds "
                    "moved. Set POLYECHO_LIVE_TRADING=true to enable."
                ),
                sell_match=sell_match,
            )

        # --- LIVE PATH (only reached when POLYECHO_LIVE_TRADING=true) ----
        if self._client_factory is None:  # pragma: no cover - live-only
            raise RuntimeError(
                "live trading enabled but no client_factory configured"
            )
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):  # pragma: no cover - live-only
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
            except Exception as exc:  # noqa: BLE001 - retry ≤2, re-sign
                last_exc = exc
                logger.warning(
                    "order_place_retry",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
        return ExecutionResult(  # pragma: no cover - live-only
            placed=False,
            decision=decision,
            skip_reason=SkipReason.DATA_UNAVAILABLE,
            detail=(
                f"order placement failed after "
                f"{self._max_retries + 1} attempts: {last_exc}"
            ),
            sell_match=sell_match,
        )
