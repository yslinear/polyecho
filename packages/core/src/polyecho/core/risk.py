"""
polyecho.core.risk
==================
Fail-closed layered pre-trade :class:`RiskGate` (PolyHermes brief §4/§7).

Gate order — **cheap → expensive, ONE reused orderbook fetch,
data-unavailable ⇒ reject** (brief §4):

    1.  dup                       (already-processed tx)
    2.  not-following             (no active follow relation)
    3.  keyword                   (category / keyword filter)
    4.  market-end                (market already ended / too short)
    5.  price-band                (price outside template band)
    6.  spread                    (orderbook spread too wide)      ┐ ONE
    7.  depth                     (orderbook depth too thin)        ├ orderbook
    8.  position-value            (per-position value cap)         │ fetch
    9.  size-normalize            (adjust/clamp; skip if infeasible)│ reused
    10. daily-order-cap           (per-day order count cap)        │
    11. daily-loss-cap            (per-day realised+open loss)      │
    12. portfolio-cap             (cross-leader/account exposure)   │  brief §7
    13. mtm/open-loss breaker     (mark-to-market circuit breaker)  │  improve-
    14. position-count/concurrency cap (KEEP this — brief §7 #4)    │  ments
    15. slippage-vs-best-ask      (slippage tolerance)             ┘
    16. strategy-drift            (leader drifted from profile)

The brief §7 safety improvements over PolyHermes implemented in / around
this gate (the other two live in detection.py / reconcile.py):

* #2 mark-to-market / open-loss circuit breaker (gate 13 — NOT
  realised-only),
* #3 cross-leader / cross-account portfolio-exposure cap (gate 12),
* #4 KEEP a position-count / concurrency cap (gate 14),
* #6 every reject writes a queryable ``copy_skips`` row with the precise
  :class:`SkipReason` (the executor persists what this gate returns).

**Fail-closed**: every required position / orderbook / market fetch is
wrapped so any exception ⇒ ``SkipReason.DATA_UNAVAILABLE`` reject (brief
§4).  The single orderbook fetch is performed once and reused by the
spread, depth and slippage gates.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from polyecho.core.evaluator import TemplateConfig
from polyecho.shared.copytrading import (
    DecisionOutcome,
    EvaluatedCopy,
    RiskResult,
    SkipReason,
    TradeSide,
)

logger = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class RiskContext:
    """Live counters the expensive gates need (brief §4/§7).

    All values are pre-fetched once by the engine so the gate stays pure
    and deterministically unit-testable.
    """

    template: TemplateConfig
    already_processed: bool = False
    is_following: bool = True
    daily_order_count: int = 0
    daily_realised_loss_usd: Decimal = _ZERO
    open_unrealised_loss_usd: Decimal = _ZERO
    portfolio_exposure_usd: Decimal = _ZERO
    open_position_count: int = 0
    strategy_drift: bool = False


#: ``async (token_id) -> {"bids":[{price,size}...], "asks":[...]}``.
OrderbookFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class _MalformedBook(Exception):
    """Raised when an orderbook row is missing/again-shaped (fail-CLOSED).

    A real Polymarket book row that lacks ``price``/``size`` or carries an
    un-coercible value is *data unavailable*, NOT "no book" — letting the
    underlying ``KeyError``/``InvalidOperation`` escape ``check`` would
    raise OUTSIDE the gate's try/except and fail **OPEN**.  We raise this
    sentinel instead and the gate maps it to
    :attr:`SkipReason.DATA_UNAVAILABLE` (P1-2, brief §4 fail-closed).
    """


def _level(row: Any) -> tuple[Decimal, Decimal]:
    """Coerce one book row to ``(price, size)`` or raise :class:`_MalformedBook`.

    Tolerates dict rows (``{"price","size"}``) and ``[price, size]``
    pair-shaped rows; any missing field / non-numeric value ⇒ fail-CLOSED.
    """
    try:
        if isinstance(row, dict):
            price_raw = row["price"]
            size_raw = row["size"]
        else:  # list/tuple-shaped level: [price, size, ...]
            price_raw = row[0]
            size_raw = row[1]
        return Decimal(str(price_raw)), Decimal(str(size_raw))
    except Exception as exc:  # noqa: BLE001 - any shape error ⇒ fail-closed
        raise _MalformedBook(f"malformed orderbook row {row!r}: {exc}") from exc


def _best(levels: list[Any], *, side: str) -> tuple[Decimal, Decimal] | None:
    """Best (price, size) — highest bid or lowest ask.

    Raises :class:`_MalformedBook` on any malformed row (fail-CLOSED) so
    a real again-shaped Polymarket book is rejected, never silently
    treated as "no book".
    """
    if not levels:
        return None
    parsed = [_level(x) for x in levels]
    if side == "bid":
        p, s = max(parsed, key=lambda t: t[0])
    else:
        p, s = min(parsed, key=lambda t: t[0])
    return p, s


def _depth(levels: list[Any]) -> Decimal:
    return sum((_level(x)[1] for x in levels), _ZERO)


class LayeredRiskGate:
    """Concrete fail-closed :class:`~polyecho.shared.copytrading.RiskGate`.

    Args:
        context_for:    ``async (EvaluatedCopy) -> RiskContext`` — all live
                        counters, pre-fetched by the engine.
        orderbook_for:  Optional ``async (token_id) -> book`` — fetched at
                        most ONCE per check and reused (brief §4).  When
                        ``None``, the book-dependent gates are skipped (no
                        book required for the configured template).
    """

    def __init__(
        self,
        *,
        context_for: Callable[[EvaluatedCopy], Awaitable[RiskContext]],
        orderbook_for: OrderbookFetcher | None = None,
    ) -> None:
        self._context_for = context_for
        self._orderbook_for = orderbook_for

    async def check(self, evaluated: EvaluatedCopy) -> RiskResult:
        """Run the brief §4 layered gate; fail-closed on any fetch error."""
        decision = evaluated.decision
        if decision.outcome is not DecisionOutcome.COPY:
            # A SKIP decision never reaches the gate in the engine, but be
            # defensive: pass it through unchanged.
            return RiskResult(passed=True, detail="non-COPY decision passed through")

        # Fail-closed: the context fetch itself is a required fetch.
        try:
            ctx = await self._context_for(evaluated)
        except Exception as exc:  # noqa: BLE001 - fail-closed (brief §4)
            logger.warning(
                "risk_context_unavailable",
                extra={"tx_hash": evaluated.tx_hash, "error": str(exc)},
            )
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.DATA_UNAVAILABLE,
                detail=f"risk context fetch failed: {exc}",
            )

        tpl = ctx.template
        price = decision.price or _ZERO
        size = decision.size or _ZERO

        # 1. dup ----------------------------------------------------------
        if ctx.already_processed:
            return RiskResult(
                passed=False, skip_reason=SkipReason.DUP, detail="tx already processed"
            )

        # 2. not-following ------------------------------------------------
        if not ctx.is_following:
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.NOT_FOLLOWING,
                detail="follow relation no longer active",
            )

        # 3. keyword ------------------------------------------------------
        if tpl.category_whitelist:
            category = str(
                evaluated.raw.get("category") or evaluated.raw.get("title") or ""
            )
            if not any(c.lower() in category.lower() for c in tpl.category_whitelist):
                return RiskResult(
                    passed=False,
                    skip_reason=SkipReason.FILTER_KEYWORD,
                    detail=f"category {category!r} not whitelisted",
                )

        # 4. market-end (cheap re-check; primary check is in the evaluator)
        # Carried via raw["market_ended"] when the engine pre-resolved it.
        if evaluated.raw.get("market_ended") is True:
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.FILTER_MARKET_END,
                detail="market ended",
            )

        # 5. price-band ---------------------------------------------------
        if price < tpl.min_price or price > tpl.max_price:
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.FILTER_PRICE_BAND,
                detail=f"price {price} outside band",
            )

        # --- ONE orderbook fetch, reused by 6/7/15 (brief §4) -----------
        book: dict[str, Any] | None = None
        if self._orderbook_for is not None and decision.token_id is not None:
            try:
                book = await self._orderbook_for(decision.token_id)
            except Exception as exc:  # noqa: BLE001 - fail-closed (brief §4)
                logger.warning(
                    "orderbook_unavailable",
                    extra={"token_id": decision.token_id, "error": str(exc)},
                )
                return RiskResult(
                    passed=False,
                    skip_reason=SkipReason.DATA_UNAVAILABLE,
                    detail=f"orderbook fetch failed: {exc}",
                )

        best_ask: tuple[Decimal, Decimal] | None = None
        if book is not None:
            # P1-2: parsing a real malformed/again-shaped book row used to
            # raise OUTSIDE this gate's try/except (fail-OPEN).  We now
            # contain ANY shape error here and convert it to a fail-CLOSED
            # DATA_UNAVAILABLE reject, exactly like the fetch failures above
            # (brief §4 — distinguish "unavailable" from "no book").
            try:
                bids_raw = book.get("bids", [])
                asks_raw = book.get("asks", [])
                if not isinstance(bids_raw, (list, tuple)) or not isinstance(
                    asks_raw, (list, tuple)
                ):
                    raise _MalformedBook(
                        f"orderbook bids/asks not list-shaped: "
                        f"{type(bids_raw).__name__}/{type(asks_raw).__name__}"
                    )
                bids = list(bids_raw)
                asks = list(asks_raw)
                best_bid = _best(bids, side="bid")
                best_ask = _best(asks, side="ask")
                buy_depth = _depth(asks) if tpl.min_depth > _ZERO else _ZERO
                sell_depth = _depth(bids) if tpl.min_depth > _ZERO else _ZERO
            except _MalformedBook as exc:
                logger.warning(
                    "orderbook_malformed",
                    extra={"token_id": decision.token_id, "error": str(exc)},
                )
                return RiskResult(
                    passed=False,
                    skip_reason=SkipReason.DATA_UNAVAILABLE,
                    detail=f"orderbook malformed: {exc}",
                )

            # 6. spread ---------------------------------------------------
            if best_bid is not None and best_ask is not None:
                spread = best_ask[0] - best_bid[0]
                if spread > tpl.max_spread:
                    return RiskResult(
                        passed=False,
                        skip_reason=SkipReason.ORDERBOOK_SPREAD,
                        detail=f"spread {spread} > max {tpl.max_spread}",
                    )

            # 7. depth ----------------------------------------------------
            if tpl.min_depth > _ZERO:
                relevant_depth = (
                    buy_depth if decision.side is TradeSide.BUY else sell_depth
                )
                if relevant_depth < tpl.min_depth:
                    return RiskResult(
                        passed=False,
                        skip_reason=SkipReason.ORDERBOOK_DEPTH,
                        detail=f"depth {relevant_depth} < min {tpl.min_depth}",
                    )

        # 8. position-value cap ------------------------------------------
        notional = size * price
        if tpl.max_position_usd is not None and notional > tpl.max_position_usd:
            # 9. size-normalize: adjust, skip only if infeasible (brief §4).
            if price > _ZERO:
                # C2 (R7): ROUND_DOWN — Python's Decimal default is
                # ROUND_HALF_EVEN, which can round the size UP and make
                # ``adjusted * price`` exceed ``max_position_usd`` (the cap
                # we are enforcing).  Always round toward zero so the
                # normalised size never breaches the per-position USD cap;
                # this also matches the evaluator's sizing quantum
                # (evaluator.py uses ``quantize(_QUANT, rounding=ROUND_DOWN)``).
                adjusted = (tpl.max_position_usd / price).quantize(
                    Decimal("0.000001"), rounding=ROUND_DOWN
                )
                if adjusted >= _ONE and (
                    tpl.min_trade_size_usd is None
                    or adjusted * price >= tpl.min_trade_size_usd
                ):
                    size = adjusted
                    notional = size * price
                else:
                    return RiskResult(
                        passed=False,
                        skip_reason=SkipReason.POSITION_CAP,
                        detail="position-value cap infeasible after normalize",
                    )
            else:
                return RiskResult(
                    passed=False,
                    skip_reason=SkipReason.POSITION_CAP,
                    detail="position-value cap, zero price",
                )

        # 10. daily-order-cap --------------------------------------------
        if (
            tpl.daily_order_cap is not None
            and ctx.daily_order_count >= tpl.daily_order_cap
        ):
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.DAILY_ORDER_CAP,
                detail=f"daily orders {ctx.daily_order_count} ≥ {tpl.daily_order_cap}",
            )

        # 11. daily-loss-cap (realised — but see 13 for the MtM breaker) --
        if (
            tpl.daily_loss_limit_usd is not None
            and ctx.daily_realised_loss_usd >= tpl.daily_loss_limit_usd
        ):
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.DAILY_LOSS_CAP,
                detail=(
                    f"daily realised loss ${ctx.daily_realised_loss_usd} "
                    f"≥ ${tpl.daily_loss_limit_usd}"
                ),
            )

        # 12. portfolio cap — cross-leader/account (brief §7 #3) ----------
        if (
            tpl.portfolio_max_usd is not None
            and ctx.portfolio_exposure_usd + notional > tpl.portfolio_max_usd
        ):
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.PORTFOLIO_CAP,
                detail=(
                    f"portfolio ${ctx.portfolio_exposure_usd} + ${notional} "
                    f"> cap ${tpl.portfolio_max_usd}"
                ),
            )

        # 13. mark-to-market / open-loss circuit breaker (brief §7 #2) ---
        # NOT realised-only: realised + open unrealised drawdown together.
        if tpl.open_loss_limit_usd is not None:
            combined = ctx.daily_realised_loss_usd + ctx.open_unrealised_loss_usd
            if combined >= tpl.open_loss_limit_usd:
                return RiskResult(
                    passed=False,
                    skip_reason=SkipReason.DAILY_LOSS_CAP,
                    detail=(
                        f"MtM breaker: realised ${ctx.daily_realised_loss_usd} "
                        f"+ open ${ctx.open_unrealised_loss_usd} "
                        f"≥ ${tpl.open_loss_limit_usd}"
                    ),
                )

        # 14. position-count / concurrency cap — KEEP (brief §7 #4) ------
        if (
            tpl.max_concurrent_positions is not None
            and ctx.open_position_count >= tpl.max_concurrent_positions
        ):
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.POSITION_CAP,
                detail=(
                    f"open positions {ctx.open_position_count} "
                    f"≥ max {tpl.max_concurrent_positions}"
                ),
            )

        # 15. slippage-vs-best-ask (brief §3/§4) -------------------------
        if best_ask is not None and decision.side is TradeSide.BUY and price > _ZERO:
            ask_price = best_ask[0]
            if ask_price > _ZERO:
                slip_pct = (ask_price - price) / ask_price * _HUNDRED
                # Negative slip = our limit ≥ ask = fine; positive = ask
                # above our limit ⇒ won't fill at tolerance.
                tol = tpl.max_slippage_pct
                if slip_pct > tol:
                    return RiskResult(
                        passed=False,
                        skip_reason=SkipReason.SLIPPAGE_REJECT,
                        detail=(
                            f"best ask {ask_price} vs limit {price} "
                            f"slippage {slip_pct:.2f}% > tol {tol}%"
                        ),
                    )

        # 16. strategy-drift (brief §7) ----------------------------------
        if ctx.strategy_drift:
            return RiskResult(
                passed=False,
                skip_reason=SkipReason.STRATEGY_DRIFT,
                detail="leader strategy drifted from followed profile",
            )

        adjusted_size = size if size != (decision.size or _ZERO) else None
        return RiskResult(
            passed=True,
            adjusted_size=adjusted_size,
            detail="all risk gates passed",
        )
