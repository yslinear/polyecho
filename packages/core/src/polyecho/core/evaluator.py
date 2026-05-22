"""
polyecho.core.evaluator
========================
:class:`CopyEvaluator` implementation (PolyHermes brief §3, PRD §5.5).

Pure decision logic — **no order placement, no risk gate** (that is
:class:`polyecho.core.risk.LayeredRiskGate`).  Given a detected leader
trade plus the resolved :class:`~polyecho.shared.copytrading.WalletBinding`,
it runs the brief §3 pipeline up to the decision:

    dedup-context → BUY/SELL branch → compute size → cheap template
    filters → build a FOK :class:`~polyecho.shared.copytrading.Decision`

Sizing (brief §3): ``copy_ratio`` ⇒ ``leaderSize × ratio``; ``fixed`` ⇒
``fixed_size_usd / price``; clamp to ≥ 1 share and to the template
min/max; below the venue/template minimum ⇒ ``SkipReason.BELOW_MIN_SIZE``.

The frozen contract is honoured verbatim: the public coroutine signature
is exactly ``CopyEvaluator.evaluate_copy`` and the returned object is a
:class:`~polyecho.shared.copytrading.EvaluatedCopy`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from polyecho.shared.copytrading import (
    Decision,
    DecisionOutcome,
    EvaluatedCopy,
    SkipReason,
    TradeSide,
    WalletBinding,
)

logger = logging.getLogger(__name__)

_ONE = Decimal("1")
_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class TemplateConfig:
    """Snapshot of the copy-template fields the evaluator + gate read.

    The brief (§4) snapshots ``copy_templates`` into the follow relation on
    apply (decoupled, not FK-bound); this is that immutable snapshot the
    engine carries per ``(account, leader)``.
    """

    copy_ratio: Decimal | None = None
    fixed_size_usd: Decimal | None = None
    max_position_usd: Decimal | None = None
    min_trade_size_usd: Decimal | None = None
    max_concurrent_positions: int | None = None
    daily_loss_limit_usd: Decimal | None = None
    category_whitelist: tuple[str, ...] = ()
    exclude_short_markets: bool = True
    use_fok_orders: bool = True
    # Tri-mode keyword filter (PolyHermes deep-dive B4). ``keyword_filter_mode``
    # is ``disabled`` (no gating — the default, so existing templates behave
    # exactly as before) | ``whitelist`` (copy only if a term matches) |
    # ``blacklist`` (skip if a term matches). ``keywords`` is the operator
    # term list. The legacy ``category_whitelist`` is honoured additively: a
    # non-empty whitelist still gates as before, regardless of mode.
    keyword_filter_mode: str = "disabled"
    keywords: tuple[str, ...] = ()
    # Price band / book guards (brief §4 Conservative ceilings as defaults).
    min_price: Decimal = Decimal("0.02")
    max_price: Decimal = Decimal("0.98")
    max_spread: Decimal = Decimal("0.10")
    min_depth: Decimal = Decimal("0")
    price_tolerance_pct: Decimal = Decimal("3")
    max_slippage_pct: Decimal = Decimal("3")
    # Market-end bounds: ``market_min_end_seconds`` is the lower bound (skip a
    # market resolving too soon); ``market_max_end_seconds`` is the upper
    # bound (skip a market resolving too far out). 0 / None disables each.
    market_min_end_seconds: int = 0
    market_max_end_seconds: int | None = None
    # Portfolio / circuit-breaker (brief §7 improvements 2 & 3).
    portfolio_max_usd: Decimal | None = None
    open_loss_limit_usd: Decimal | None = None
    daily_order_cap: int | None = None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class TraderCopyEvaluator:
    """Concrete :class:`~polyecho.shared.copytrading.CopyEvaluator`.

    ``template_for`` / ``keyword_excluded`` / ``market_end`` are injected so
    the evaluator stays pure and unit-testable without DB or network.
    """

    def __init__(
        self,
        *,
        template_for: Any,
        market_end_for: Any | None = None,
        is_following: Any | None = None,
    ) -> None:
        # template_for(account_id, leader) -> TemplateConfig | None (sync)
        self._template_for = template_for
        # market_end_for(condition_id) -> datetime | None (sync)
        self._market_end_for = market_end_for
        # is_following(account_id, leader) -> bool (sync); default: True
        self._is_following = is_following

    async def evaluate_copy(
        self,
        *,
        leader_address: str,
        tx_hash: str,
        account_id: uuid.UUID,
        condition_id: str,
        binding: WalletBinding,
        trade: Mapping[str, Any],
    ) -> EvaluatedCopy:
        """Decide what to do with one detected leader trade (brief §3).

        Returns a fully-decided :class:`EvaluatedCopy`; ``risk`` is left
        ``None`` (the gate runs separately, per the frozen contract).
        """

        def _skip(reason: SkipReason, detail: str) -> EvaluatedCopy:
            return EvaluatedCopy(
                leader_address=leader_address,
                tx_hash=tx_hash,
                account_id=account_id,
                condition_id=condition_id,
                binding=binding,
                decision=Decision(
                    outcome=DecisionOutcome.SKIP,
                    skip_reason=reason,
                    detail=detail,
                ),
                raw=dict(trade),
            )

        # --- not-following branch (brief §4) ---------------------------------
        if self._is_following is not None and not self._is_following(account_id, leader_address):
            return _skip(
                SkipReason.NOT_FOLLOWING,
                f"no active follow_relation for {leader_address}",
            )

        tpl: TemplateConfig | None = self._template_for(account_id, leader_address)
        if tpl is None:
            return _skip(
                SkipReason.NOT_FOLLOWING,
                f"no copy_template snapshot for account {account_id} "
                f"leader {leader_address}; re-promote or reattach template",
            )

        side_raw = str(trade.get("side", "")).upper()
        if side_raw not in ("BUY", "SELL"):
            return _skip(
                SkipReason.DATA_UNAVAILABLE,
                f"leader trade side {trade.get('side')!r} is not BUY/SELL",
            )
        side = TradeSide(side_raw)

        # --- keyword / category filter (brief §4, PolyHermes deep-dive B4) ---
        # The market text matched against both term lists. PolyHermes matches
        # title + category; we concatenate both so either field can match.
        market_text = " ".join(str(trade.get(k) or "") for k in ("category", "title")).lower()

        # Legacy category_whitelist: kept additive — a non-empty whitelist
        # still gates as before, independent of keyword_filter_mode.
        if tpl.category_whitelist:
            if not any(c.lower() in market_text for c in tpl.category_whitelist):
                return _skip(
                    SkipReason.FILTER_KEYWORD,
                    f"market text {market_text!r} not in category_whitelist",
                )

        # Tri-mode keyword filter over the operator term list. ``disabled``
        # (default) does no gating, so existing templates are unchanged.
        mode = (tpl.keyword_filter_mode or "disabled").lower()
        if mode in ("whitelist", "blacklist") and tpl.keywords:
            matched = any(kw.lower() in market_text for kw in tpl.keywords)
            if mode == "whitelist" and not matched:
                return _skip(
                    SkipReason.FILTER_KEYWORD,
                    f"market text {market_text!r} matches no whitelist keyword",
                )
            if mode == "blacklist" and matched:
                return _skip(
                    SkipReason.FILTER_KEYWORD,
                    f"market text {market_text!r} matches a blacklist keyword",
                )

        # --- short-market / market-end filter (brief §4, B4 upper bound) -----
        if self._market_end_for is not None:
            end_dt: datetime | None = self._market_end_for(condition_id)
            if end_dt is not None:
                now = datetime.now(tz=UTC)
                if end_dt <= now:
                    return _skip(
                        SkipReason.FILTER_MARKET_END,
                        f"market already ended at {end_dt.isoformat()}",
                    )
                remaining = (end_dt - now).total_seconds()
                if tpl.exclude_short_markets and tpl.market_min_end_seconds > 0:
                    if remaining < tpl.market_min_end_seconds:
                        return _skip(
                            SkipReason.FILTER_MARKET_END,
                            f"market ends in {remaining:.0f}s < {tpl.market_min_end_seconds}s",
                        )
                # Upper bound (PolyHermes maxMarketEndDate): skip a market
                # resolving further out than the operator ceiling.
                if (
                    tpl.market_max_end_seconds is not None
                    and tpl.market_max_end_seconds > 0
                    and remaining > tpl.market_max_end_seconds
                ):
                    return _skip(
                        SkipReason.FILTER_MARKET_END,
                        f"market ends in {remaining:.0f}s > {tpl.market_max_end_seconds}s",
                    )

        # --- price band (brief §4) ------------------------------------------
        price = _to_decimal(trade.get("price"))
        if price is None or price <= 0:
            return _skip(
                SkipReason.DATA_UNAVAILABLE,
                f"leader trade has no usable price (got {trade.get('price')!r}); "
                "expected a positive number",
            )
        if price < tpl.min_price or price > tpl.max_price:
            return _skip(
                SkipReason.FILTER_PRICE_BAND,
                f"price {price} outside [{tpl.min_price}, {tpl.max_price}]",
            )

        # --- sizing (brief §3) ----------------------------------------------
        leader_size = _to_decimal(trade.get("size")) or Decimal(0)
        if tpl.fixed_size_usd is not None:
            target = (tpl.fixed_size_usd / price) if price > 0 else Decimal(0)
        elif tpl.copy_ratio is not None:
            target = leader_size * tpl.copy_ratio
        else:
            return _skip(
                SkipReason.DATA_UNAVAILABLE,
                "copy_template has neither copy_ratio nor fixed_size_usd; set one to enable sizing",
            )

        # Clamp by max_position_usd (value cap, brief §3/§4).
        if tpl.max_position_usd is not None and price > 0:
            max_shares = tpl.max_position_usd / price
            if target > max_shares:
                target = max_shares

        # Round down to whole shares, clamp ≥ 1 share (brief §3).
        size = target.quantize(_QUANT, rounding=ROUND_DOWN)
        if size < _ONE:
            return _skip(
                SkipReason.BELOW_MIN_SIZE,
                f"computed size {size} is below the venue minimum of 1 share",
            )

        notional = size * price
        if tpl.min_trade_size_usd is not None and notional < tpl.min_trade_size_usd:
            return _skip(
                SkipReason.BELOW_MIN_SIZE,
                f"notional ${notional} < min_trade_size_usd ${tpl.min_trade_size_usd}",
            )

        token_id = str(trade.get("token_id") or trade.get("asset") or condition_id)
        order_type = "FOK" if tpl.use_fok_orders else "FAK"
        decision = Decision(
            outcome=DecisionOutcome.COPY,
            side=side,
            token_id=token_id,
            price=price,
            size=size,
            order_type=order_type,
            detail=f"copy {side.value} {size}@{price} (leader {leader_address})",
        )
        return EvaluatedCopy(
            leader_address=leader_address,
            tx_hash=tx_hash,
            account_id=account_id,
            condition_id=condition_id,
            binding=binding,
            decision=decision,
            raw=dict(trade),
        )
