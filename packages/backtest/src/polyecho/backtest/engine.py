"""Backtest engine: simulates copy-trading PnL with slippage, delay, and risk models.

PRD reference: §5.4 (backtest engine inputs, simulation logic, outputs).

Simulation steps (PRD §5.4):
1. Fetch all leader TRADE activity for [start_date, end_date] via DataSource.
2. Replay trades in chronological order:
   a. Category whitelist filter (skip if market category not allowed).
   b. Risk checks: daily loss limit, max concurrent positions.
   c. Compute copy size (copy_ratio × leader usdc_size OR fixed_size_usd).
   d. Below-min filter: skip if copy size < min_trade_size_usd.
   e. Liquidity proxy: skip if copy size > 10 × leader usdc_size (thin market).
   f. Model fill price = leader price ± slippage_bps (adverse direction).
      Execution delay is a no-op in the price model because fixture data has
      no intra-second order-book data; we model it as an *additional* adverse
      slippage proxy: delay_slippage_bps = execution_delay_seconds × 0.5 bps/s,
      so a 30 s delay adds another 15 bps on top of slippage_bps. Documented
      as ASSUMPTION-DELAY below.
   g. PnL for BUY trades: each BUY opens a FIFO lot at its own fill price;
      a SELL consumes the oldest still-open lots first (strict FIFO cost
      basis, PolyHermes' SellMatchDetail). Realised PnL is the sum over the
      consumed lots of (sell_price - lot_buy_price) * matched_qty. A SELL
      larger than the remaining open lots is matched only up to the open
      quantity (the surplus is dropped, not shorted). Any lots still open at
      period end are marked-to-market.
3. Mark-to-market all still-open lots using the last in-window fill price as
   a proxy for last known price (look-ahead-bias-safe).
4. Compute daily cumulative PnL curve + summary statistics.

FIFO lot model (B5, PolyHermes deep-dive):
  A leader who layers partial BUYs at different prices and then partial
  SELLs cannot be priced by a single whole-position close keyed on
  condition_id: the cost basis of each closed slice depends on WHICH lot it
  consumed. The engine therefore tracks, per condition_id, an ordered list
  of open lots (each = one BUY's residual shares + that BUY's fill price +
  its committed notional). A SELL walks that list oldest-first, consuming
  shares lot by lot until the SELL size is exhausted or all lots are empty,
  and the realised PnL is the per-lot sum. This is fully consistent with
  the existing slippage / per-leg fee / 3-scenario modelling.

Key modeling assumptions (labelled inline):
  ASSUMPTION-DELAY   : No intra-bar fill-price movement available; execution
                       delay is modelled as extra adverse slippage at 0.5 bps/s.
  ASSUMPTION-LIQ     : Liquidity gate rejects copy size > 10× leader usdc_size
                       because thin markets cannot absorb disproportionate size.
  ASSUMPTION-MTM     : End-of-period MTM uses PositionRecord.current_value /
                       position.size as the "last price"; if no open position
                       record exists we use the last known fill price.
  ASSUMPTION-SHARPE  : Risk-free rate = 0 (appropriate for prediction markets).
  ASSUMPTION-ANNUALIZE: Annualisation uses 365 calendar days.
  ASSUMPTION-COPY-SIZE: copy_ratio and fixed_size_usd are mutually exclusive;
                       if fixed_size_usd > 0, it overrides copy_ratio entirely.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from polyecho.shared.datasource import ActivityRecord, DataSource, PositionRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter dataclass (EXACT fields from PRD §5.4)
# ---------------------------------------------------------------------------


@dataclass
class BacktestParams:
    """Input parameters for a backtest run.

    All fields and defaults are taken verbatim from PRD §5.4.

    Attributes:
        leader_address:          Wallet address of the leader to simulate.
        start_date:              Inclusive start date of the simulation window.
        end_date:                Inclusive end date of the simulation window.
        initial_capital_usd:     Starting capital in USD.
        copy_ratio:              Fraction of leader's usdc_size to copy
                                 (e.g. 0.001 = 0.1%).  Mutually exclusive
                                 with fixed_size_usd (ASSUMPTION-COPY-SIZE).
        fixed_size_usd:          If > 0, override copy_ratio with this fixed
                                 dollar amount per trade.  0 means disabled.
        max_position_usd:        Maximum single-position size in USD.
        min_trade_size_usd:      Minimum copy size; smaller trades are skipped.
        daily_loss_limit_usd:    Stop copying for the rest of the day if
                                 cumulative daily loss exceeds this value.
        max_concurrent_positions: Maximum number of open positions allowed.
        slippage_bps:            Adverse slippage in basis points (1 bp = 0.01%).
                                 Default 150 (1.5%) per PRD §5.4.
        fee_bps:                 Per-leg trading fee in basis points, charged
                                 as ``copy_usdc * fee_bps / 10_000`` on every
                                 BUY open AND every SELL close (PRD §5.4
                                 non-negotiable — backtest MUST model fee).
                                 Default 0 (Polymarket's headline taker fee is
                                 currently 0 bps; operators set this to model a
                                 conservative scenario or future fee schedule).
        execution_delay_seconds: Simulated execution delay in seconds.
                                 Modelled as extra adverse slippage at
                                 0.5 bps/s (ASSUMPTION-DELAY).
                                 Default 30 s per PRD §5.4.
        category_whitelist:      If set, only trades on markets whose
                                 condition_id title contains one of these
                                 strings (case-insensitive) are copied.
                                 None means all categories allowed.
    """

    leader_address: str
    start_date: date
    end_date: date
    initial_capital_usd: float
    copy_ratio: float  # 0.001 = follow 0.1% of leader's size
    max_position_usd: float
    min_trade_size_usd: float
    daily_loss_limit_usd: float
    max_concurrent_positions: int = 10
    fixed_size_usd: float = 0.0  # 0 = disabled; use copy_ratio instead
    slippage_bps: int = 150  # default 1.5 %
    fee_bps: int = 0  # per-leg trading fee (BUY open + SELL close); PRD §5.4
    execution_delay_seconds: int = 30
    category_whitelist: list[str] | None = None


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class SkipStats:
    """Breakdown of skipped copy-trades by reason (PRD §5.4 output)."""

    insufficient_liquidity: int = 0
    risk_triggered: int = 0
    below_min_size: int = 0
    category_filtered: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "insufficient_liquidity": self.insufficient_liquidity,
            "risk_triggered": self.risk_triggered,
            "below_min_size": self.below_min_size,
            "category_filtered": self.category_filtered,
            "total": self.total,
        }


@dataclass
class BacktestResult:
    """Output of a completed backtest run.

    PRD §5.4 output:
    - daily_pnl_curve: list of (date_str, cumulative_pnl_usd) pairs.
    - stats: total_return, annualized, sharpe, max_drawdown, win_rate,
             avg_hold_hours.
    - skip_analysis: liquidity / risk / below_min / category counts.
    - leader_vs_copy: comparison dict.
    """

    params: BacktestParams

    # Cumulative PnL curve — index is date, values are USD cumulative PnL
    pnl_curve: pd.Series = field(default_factory=pd.Series)

    # Aggregated statistics
    total_return_usd: float = 0.0
    annualised_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    avg_hold_hours: float = 0.0
    total_fees_usd: float = 0.0

    # Skip analysis
    skip_stats: SkipStats = field(default_factory=SkipStats)

    # Leader's own PnL over the same period (for comparison)
    leader_pnl_curve: pd.Series = field(default_factory=pd.Series)
    leader_total_return_usd: float = 0.0

    # Raw trade log: list of dicts with per-trade detail
    trade_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSONB-compatible dict for storage in backtest_jobs.result."""

        # Convert pnl_curve pd.Series → list of [date_str, value] pairs
        def series_to_list(s: pd.Series) -> list[list[Any]]:
            if s.empty:
                return []
            return [[str(idx), float(v)] for idx, v in s.items()]

        return {
            "params": {
                "leader_address": self.params.leader_address,
                "start_date": str(self.params.start_date),
                "end_date": str(self.params.end_date),
                "initial_capital_usd": self.params.initial_capital_usd,
                "copy_ratio": self.params.copy_ratio,
                "fixed_size_usd": self.params.fixed_size_usd,
                "max_position_usd": self.params.max_position_usd,
                "min_trade_size_usd": self.params.min_trade_size_usd,
                "daily_loss_limit_usd": self.params.daily_loss_limit_usd,
                "max_concurrent_positions": self.params.max_concurrent_positions,
                "slippage_bps": self.params.slippage_bps,
                "fee_bps": self.params.fee_bps,
                "execution_delay_seconds": self.params.execution_delay_seconds,
                "category_whitelist": self.params.category_whitelist,
            },
            "daily_pnl_curve": series_to_list(self.pnl_curve),
            "stats": {
                "total_return_usd": self.total_return_usd,
                "annualized_return_pct": self.annualised_return_pct,
                "sharpe_ratio": self.sharpe_ratio,
                "max_drawdown_pct": self.max_drawdown_pct,
                "win_rate": self.win_rate,
                "avg_hold_hours": self.avg_hold_hours,
                "total_fees_usd": self.total_fees_usd,
            },
            "skip_analysis": self.skip_stats.to_dict(),
            # B1 (PolyHermes deep-dive): the full per-fill trade log is
            # emitted here so the jobs path can fan it out into the
            # backtest_trades per-fill ledger table. Additive — every
            # existing ``result`` JSONB consumer keeps working unchanged.
            "trade_log": list(self.trade_log),
            "leader_vs_copy": {
                "leader_pnl_curve": series_to_list(self.leader_pnl_curve),
                "leader_total_return_usd": self.leader_total_return_usd,
                "copy_total_return_usd": self.total_return_usd,
            },
        }


# ---------------------------------------------------------------------------
# Pure helper functions (testable independently)
# ---------------------------------------------------------------------------


def apply_slippage(price: float, side: str, slippage_bps: int) -> float:
    """Compute the simulated fill price after adverse slippage.

    PRD §5.4: BUY gets a worse (higher) price; SELL gets a worse (lower) price.
    Result is clamped to [0.0, 1.0] because Polymarket prices are probabilities.

    Args:
        price:        Mid-market price at fill time (0–1 for binary markets).
        side:         "BUY" or "SELL".
        slippage_bps: Slippage in basis points (1 bps = 0.01%).

    Returns:
        Adjusted fill price clamped to [0.0, 1.0].
    """
    factor = slippage_bps / 10_000.0
    if side == "BUY":
        return min(1.0, price * (1.0 + factor))
    return max(0.0, price * (1.0 - factor))


def compute_fee(copy_usdc: float, fee_bps: int) -> float:
    """Trading fee for one leg of a copy trade (PRD §5.4 — non-negotiable).

    The fee is a flat basis-point charge on the *notional* USDC of the leg,
    deducted on every BUY open and every SELL close.  It is a pure cost: it
    never improves PnL.

    Args:
        copy_usdc: Notional USDC of this leg (already capped at
                   ``max_position_usd``).
        fee_bps:   Fee in basis points (1 bp = 0.01%).  0 disables the fee.

    Returns:
        Fee cost in USD (>= 0.0).
    """
    if fee_bps <= 0 or copy_usdc <= 0.0:
        return 0.0
    return copy_usdc * fee_bps / 10_000.0


def compute_settlement_price(
    last_price: float,
    *,
    resolved: bool | None = None,
    voided: bool | None = None,
    entry_price: float = 0.0,
) -> float:
    """Snap a mark-to-market price to its settlement value (PRD §5.4).

    Polymarket binary markets settle at exactly $1 (the winning outcome) or
    $0 (the losing outcome).  Near a resolution the live mid price collapses
    toward one of those poles, so the engine must *snap*:

    * last price >= 0.95  → $1.00  (treated as resolved-YES)
    * last price <= 0.05  → $0.00  (treated as resolved-NO)
    * 0.05 < last price < 0.95 → unchanged live MTM (market still open)
    * ``voided``/invalid market → return ``entry_price`` so the position's
      MTM PnL is exactly zero (the trade is effectively unwound at cost).

    ``resolved``/``voided`` are sourced from the position/market ``raw`` blob
    when available (live data); fixtures carry no resolution field, so the
    price-band rule is the load-bearing path there.  An explicit
    ``resolved=True`` still applies the price-band snap; ``voided=True`` wins
    over everything (a voided market never pays out).

    Args:
        last_price:  Live mark-to-market price (0–1).
        resolved:    Tri-state resolution flag from ``raw`` (None = unknown).
        voided:      Tri-state void/invalid flag from ``raw`` (None = unknown).
        entry_price: The position's own entry fill price (zero-MTM fallback
                     for voided markets).

    Returns:
        Settlement-snapped price in [0.0, 1.0].
    """
    if voided:
        # Voided / invalid market: no payout, unwind at cost → zero MTM PnL.
        return float(entry_price)
    if last_price >= 0.95:
        return 1.0
    if last_price <= 0.05:
        return 0.0
    return float(last_price)


def compute_sharpe(daily_pnl: list[float], *, risk_free: float = 0.0) -> float:
    """Compute annualised Sharpe ratio from daily PnL values.

    ASSUMPTION-SHARPE: risk_free = 0.

    Args:
        daily_pnl:   List of daily incremental PnL values.
        risk_free:   Daily risk-free rate (default 0).

    Returns:
        Annualised Sharpe ratio, or 0.0 if insufficient data.
    """
    arr = np.array(daily_pnl, dtype=float)
    if len(arr) < 2:
        return 0.0
    excess = arr - risk_free
    std = float(np.std(excess, ddof=1))
    if std == 0.0:
        return 0.0
    return float(np.mean(excess) / std * math.sqrt(252))


def compute_max_drawdown(cumulative_pnl: list[float]) -> float:
    """Compute maximum drawdown as a percentage of peak cumulative PnL.

    Returns a positive percentage (e.g. 12.5 means 12.5% drawdown from peak).

    The input is the cumulative-PnL-above-initial-capital curve produced by
    :meth:`BacktestEngine._build_pnl_curve` (it starts at 0.0 and may go
    negative when the strategy loses money).  Drawdown is, by definition, the
    decline from a running high-water mark expressed as a percentage *of that
    high-water mark*.  When the running peak is **not strictly positive** the
    percentage is mathematically undefined — you cannot be "down 50 % from a
    peak of $0" — so any decline occurring before the curve first rises above
    0.0 contributes no percentage drawdown.  The previous implementation
    divided the dollar decline by ``abs(peak)`` (or used ``abs(v) * 100``
    when ``peak == 0``), which produced absurd, unbounded results such as a
    5000 % "drawdown" for the perfectly ordinary curve ``[0.0, -50.0]``.

    Args:
        cumulative_pnl: List of cumulative PnL values in USD.

    Returns:
        Max drawdown percentage (>= 0.0), or 0.0 if no positive-peak
        drawdown occurred.
    """
    if not cumulative_pnl:
        return 0.0
    arr = np.array(cumulative_pnl, dtype=float)
    peak = arr[0]
    max_dd = 0.0
    for v in arr:
        if v > peak:
            peak = v
        # Drawdown as a percentage of the high-water mark is only defined
        # while that high-water mark is strictly positive.
        if peak > 0.0:
            dd = (peak - v) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return float(max_dd)


# ---------------------------------------------------------------------------
# FIFO lot-level cost basis (B5, PolyHermes' SellMatchDetail)
# ---------------------------------------------------------------------------


@dataclass
class Lot:
    """One still-open BUY lot inside a FIFO position (B5).

    A position on a single ``condition_id`` is an *ordered* list of these
    lots — each is one BUY's residual (not-yet-sold) shares, the price that
    BUY filled at, and the USDC notional that BUY committed.  A SELL consumes
    lots oldest-first; ``shares`` shrinks as a lot is partially matched and
    the lot is dropped once it reaches zero.

    Attributes:
        shares:    Residual unmatched shares of this lot (>= 0).
        fill_price: The BUY fill price for this lot (cost basis, 0-1).
        copy_usdc: USDC notional this BUY committed for these shares.  When a
                   lot is partially closed the proportional share of this
                   notional is returned to capital (see :meth:`notional_for`).
        opened_at: Timestamp of the BUY that opened this lot.
    """

    shares: float
    fill_price: float
    copy_usdc: float
    opened_at: datetime

    def notional_for(self, matched_qty: float) -> float:
        """USDC notional attributable to ``matched_qty`` shares of this lot.

        The lot's ``copy_usdc`` is the BUY notional for its *original* share
        count; a partial close returns only the proportional slice so
        ``capital_remaining`` stays exact across layered partial exits.
        """
        if self.shares <= 0.0:
            return 0.0
        frac = max(0.0, min(1.0, matched_qty / self.shares))
        return self.copy_usdc * frac


@dataclass
class SellMatch:
    """Result of matching one SELL against a FIFO lot list (B5).

    Attributes:
        matched_shares: Total shares the SELL actually closed (bounded by the
                        open lot quantity — a SELL larger than the open
                        position closes only the open part).
        gross_pnl:      Sum over consumed lots of
                        ``(sell_price - lot.fill_price) * matched_qty``.
        returned_notional: Sum of the per-lot proportional BUY notionals
                        returned to capital.
        oldest_opened_at: ``opened_at`` of the OLDEST lot the SELL touched
                        (used for the hold-time metric); None if nothing
                        matched.
    """

    matched_shares: float = 0.0
    gross_pnl: float = 0.0
    returned_notional: float = 0.0
    oldest_opened_at: datetime | None = None


def match_fifo_sell(lots: list[Lot], sell_shares: float, sell_price: float) -> SellMatch:
    """Consume ``sell_shares`` from ``lots`` oldest-first (strict FIFO).

    PolyHermes' ``SellMatchDetail``: a SELL is filled against the open BUY
    lots in the order they were opened.  Each lot contributes
    ``(sell_price - lot.fill_price) * matched_qty`` to gross realised PnL and
    its proportional BUY notional to the capital returned.  Fully-consumed
    lots are removed from ``lots`` IN PLACE; a partially-consumed lot's
    ``shares`` and ``copy_usdc`` are reduced to the residual.

    A SELL larger than the total open quantity matches only up to that
    quantity (the surplus is dropped — the engine never models a short).

    Args:
        lots:        Ordered open-lot list for one condition_id (oldest first).
                     MUTATED in place: consumed lots are removed / shrunk.
        sell_shares: Shares the SELL wants to close (>= 0).
        sell_price:  The (already slippage-adjusted) SELL fill price.

    Returns:
        A :class:`SellMatch` describing the matched quantity, gross PnL and
        returned notional.
    """
    match = SellMatch()
    remaining = max(0.0, sell_shares)
    while remaining > 0.0 and lots:
        lot = lots[0]
        take = min(remaining, lot.shares)
        if take <= 0.0:
            lots.pop(0)
            continue
        if match.oldest_opened_at is None:
            match.oldest_opened_at = lot.opened_at
        match.matched_shares += take
        match.gross_pnl += take * (sell_price - lot.fill_price)
        match.returned_notional += lot.notional_for(take)
        # Shrink the lot's notional proportionally BEFORE shrinking shares.
        lot.copy_usdc = max(0.0, lot.copy_usdc - lot.notional_for(take))
        lot.shares -= take
        remaining -= take
        if lot.shares <= 1e-12:
            lots.pop(0)
    return match


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class BacktestEngine:
    """Executes copy-trade backtests using historical Polymarket data.

    PRD §5.4.

    Usage::

        from polyecho.shared.datasource import get_datasource
        ds = get_datasource()
        params = BacktestParams(
            leader_address="0xswing1...001",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 3, 31),
            initial_capital_usd=1000.0,
            copy_ratio=0.01,
            max_position_usd=100.0,
            min_trade_size_usd=5.0,
            daily_loss_limit_usd=50.0,
        )
        engine = BacktestEngine(datasource=ds)
        result = await engine.run(params, leader_address="0xswing1...001")
    """

    def __init__(self, datasource: DataSource) -> None:
        self._ds = datasource

    async def run(
        self,
        params: BacktestParams,
        *,
        leader_address: str | None = None,
    ) -> BacktestResult:
        """Execute a full backtest and return aggregated results.

        PRD §5.4 simulation steps implemented here.

        Args:
            params:          Fully populated BacktestParams.
            leader_address:  Override params.leader_address (used by jobs.py
                             when iterating over multiple leaders).

        Returns:
            BacktestResult with PnL curve, stats, and skip analysis.
        """
        effective_leader = leader_address or params.leader_address
        # Re-use params but with effective_leader baked in for to_dict()
        if effective_leader != params.leader_address:
            from dataclasses import replace

            params = replace(params, leader_address=effective_leader)

        logger.info(
            "backtest.run.start",
            extra={
                "leader": effective_leader,
                "start": str(params.start_date),
                "end": str(params.end_date),
                "slippage_bps": params.slippage_bps,
                "delay_s": params.execution_delay_seconds,
            },
        )

        # ---- 1. Fetch leader trades ----------------------------------------
        trades = await self._fetch_leader_trades(
            effective_leader, params.start_date, params.end_date
        )
        logger.debug("backtest.trades_fetched", extra={"count": len(trades)})

        # ---- 2. Fetch end-of-period positions for MTM ----------------------
        positions_map = await self._fetch_positions_map(effective_leader)

        # ---- 3. Simulate ---------------------------------------------------
        skip_stats = SkipStats()
        trade_log: list[dict[str, Any]] = []

        # State
        capital_remaining = params.initial_capital_usd
        # open_positions: condition_id → ordered list of FIFO Lots (B5).
        # A BUY appends a Lot; a SELL consumes the oldest lots first via
        # match_fifo_sell.  A condition_id with an empty list == no open
        # position (the key is pruned so len(open_positions) stays an exact
        # open-position count for the max-concurrent-positions gate).
        open_positions: dict[str, list[Lot]] = {}
        # daily_loss: date → cumulative loss (positive = loss)
        daily_loss: dict[date, float] = defaultdict(float)
        # halted_days: days where risk limit was hit
        halted_days: set[date] = set()

        # Track leader's own cost basis for leader PnL comparison
        leader_open: dict[str, dict[str, Any]] = {}
        leader_daily_pnl: dict[date, float] = defaultdict(float)

        # Extra delay slippage: ASSUMPTION-DELAY
        # 0.5 bps per second of delay, added on top of slippage_bps
        delay_extra_bps = int(params.execution_delay_seconds * 0.5)
        total_slippage_bps = params.slippage_bps + delay_extra_bps

        for trade in trades:
            trade_date = trade.timestamp.date()
            side = trade.side or "BUY"
            leader_price = trade.price or 0.0
            leader_usdc = trade.usdc_size or 0.0
            cond_id = trade.condition_id or ""

            # ---- Track leader's own PnL (no slippage, no risk gate) --------
            self._update_leader_tracking(leader_open, leader_daily_pnl, trade, positions_map)

            # ---- C4: SELL with no matching open BUY ------------------------
            # A SELL whose condition_id is not in open_positions has no copy
            # position to close: either the leader opened the BUY *before*
            # start_date (out of window), or this is a duplicate / partial
            # SELL whose BUY was already closed.  The previous code fell
            # through to the trade-log append with side="SELL", pnl_usd=0.0
            # and skipped=False, which inflated the *closed-trade* count and
            # therefore distorted win_rate (a 0-PnL phantom "loss").  Skip it
            # BEFORE computing fill price / shares so it is recorded as a
            # genuine skip with an explicit reason and never reaches the
            # closed-trade / win-rate population.
            if side == "SELL" and cond_id not in open_positions:
                skip_stats.risk_triggered += 1
                skip_stats.total += 1
                trade_log.append(self._skip_entry(trade, "no_open_position", params))
                continue

            # ---- Category whitelist filter ---------------------------------
            if params.category_whitelist is not None:
                title = str(trade.raw.get("title") or "")
                if not any(cat.lower() in title.lower() for cat in params.category_whitelist):
                    skip_stats.category_filtered += 1
                    skip_stats.total += 1
                    trade_log.append(self._skip_entry(trade, "category_filtered", params))
                    continue

            # ---- Risk gate: daily loss limit --------------------------------
            if trade_date in halted_days:
                skip_stats.risk_triggered += 1
                skip_stats.total += 1
                trade_log.append(self._skip_entry(trade, "risk_daily_limit", params))
                continue

            # ---- Risk gate: max concurrent positions (only count BUY opens) -
            if side == "BUY" and len(open_positions) >= params.max_concurrent_positions:
                skip_stats.risk_triggered += 1
                skip_stats.total += 1
                trade_log.append(self._skip_entry(trade, "risk_max_positions", params))
                continue

            # ---- Compute copy size -----------------------------------------
            # ASSUMPTION-COPY-SIZE: fixed_size_usd > 0 overrides copy_ratio
            if params.fixed_size_usd > 0.0:
                copy_usdc = params.fixed_size_usd
            else:
                copy_usdc = leader_usdc * params.copy_ratio

            # Cap at max_position_usd
            copy_usdc = min(copy_usdc, params.max_position_usd)

            # ---- Below-min filter ------------------------------------------
            if copy_usdc < params.min_trade_size_usd:
                skip_stats.below_min_size += 1
                skip_stats.total += 1
                trade_log.append(self._skip_entry(trade, "below_min_size", params))
                continue

            # ---- Liquidity gate --------------------------------------------
            # ASSUMPTION-LIQ: reject if copy > 10× leader's own notional;
            # signals we'd dominate a thin book.
            if leader_usdc > 0.0 and copy_usdc > 10.0 * leader_usdc:
                skip_stats.insufficient_liquidity += 1
                skip_stats.total += 1
                trade_log.append(self._skip_entry(trade, "insufficient_liquidity", params))
                continue

            # ---- Adverse fill price (slippage + delay) ---------------------
            fill_price = apply_slippage(leader_price, side, total_slippage_bps)

            # ---- P1-10a: per-leg trading fee (PRD §5.4 non-negotiable) -----
            # Charged on BOTH legs: the BUY open consumes extra capital, the
            # SELL close deducts from realised PnL.  A pure cost — never
            # improves PnL — so high fee_bps must monotonically reduce return.
            #
            # CRIT-2 / B5: the *fee notional* must stay consistent with the
            # shares actually transacted on this leg.  For a BUY this is
            # ``copy_usdc`` (the opened notional).  For a SELL under FIFO the
            # gross PnL is matched only against the open lots, so the SELL-leg
            # fee is charged on the SELL-side notional bounded by the *total
            # open lot notional* on this condition_id — a SELL larger than the
            # open position is fee-charged only on the portion actually
            # closed, and a SELL that consumes layered lots at different
            # prices is fee-charged on the genuinely-matched notional.
            if side == "SELL" and cond_id in open_positions:
                _open_usdc = sum(lot.copy_usdc for lot in open_positions[cond_id])
                fee_notional = min(copy_usdc, _open_usdc)
            else:
                fee_notional = copy_usdc
            fee_usd = compute_fee(fee_notional, params.fee_bps)

            # ---- Capital check (only BUY consumes capital) -----------------
            if side == "BUY":
                # The fee is paid up-front alongside the notional, so the
                # capital gate must cover copy_usdc + fee_usd.
                if copy_usdc + fee_usd > capital_remaining:
                    # Partial fill not modelled; skip to keep it simple
                    skip_stats.risk_triggered += 1
                    skip_stats.total += 1
                    trade_log.append(self._skip_entry(trade, "risk_insufficient_capital", params))
                    continue
                capital_remaining -= copy_usdc + fee_usd

            # ---- Compute copy token size (shares = usdc / fill_price) ------
            copy_shares = copy_usdc / fill_price if fill_price > 0.0 else 0.0

            # ---- PnL realisation -------------------------------------------
            pnl_usd = 0.0
            closed_at: datetime | None = None
            opened_at: datetime | None = None

            if side == "SELL" and cond_id in open_positions:
                # B5: close against the FIFO lot list, oldest lots first.
                # match_fifo_sell mutates the lot list in place (consumed
                # lots removed / shrunk) and returns the per-lot gross PnL +
                # the proportional BUY notional returned to capital.
                lots = open_positions[cond_id]
                sell_match = match_fifo_sell(lots, copy_shares, fill_price)
                opened_at = sell_match.oldest_opened_at
                # Gross PnL = Σ over consumed lots of
                # (sell_price - lot_buy_price) * matched_qty.  The SELL-leg
                # fee is then deducted so the *net* realised PnL (and
                # daily-loss tracking) reflects the true round-trip cost.
                gross_pnl = sell_match.gross_pnl
                pnl_usd = gross_pnl - fee_usd
                # CRIT-1 / B5: the *principal returned to capital* is the sum
                # of the per-lot proportional BUY notionals that were
                # actually committed at BUY time — NOT the freshly-recomputed
                # SELL-side ``copy_usdc`` (= leader SELL notional × ratio).
                # match_fifo_sell already proportions each consumed lot's
                # notional, so a partial close returns exactly the slice that
                # was closed; capital_remaining stays exact across layered
                # partial exits.
                capital_remaining += sell_match.returned_notional + pnl_usd
                # B5: a SELL that fully drains every lot leaves an empty
                # list — prune the key so len(open_positions) stays an exact
                # open-position count for the max-concurrent gate.
                if not lots:
                    del open_positions[cond_id]
                closed_at = trade.timestamp
                # Carry the matched share count onto the trade-log entry so
                # downstream consumers see what the SELL actually closed.
                copy_shares = sell_match.matched_shares

                # Update daily loss tracking (loss = negative pnl)
                if pnl_usd < 0.0:
                    daily_loss[trade_date] -= pnl_usd  # store as positive loss amount
                    if daily_loss[trade_date] >= params.daily_loss_limit_usd:
                        halted_days.add(trade_date)
                        logger.debug(
                            "backtest.daily_limit_hit",
                            extra={"date": str(trade_date), "loss": daily_loss[trade_date]},
                        )
            elif side == "BUY":
                # Open new position. The BUY-leg fee is *already* deducted
                # from capital_remaining above; recording it on the trade-log
                # entry keeps total_fees_usd a faithful both-legs total.
                #
                # IMP-1: the BUY-leg fee is a *realised* capital outflow the
                # moment the position opens, but ``_build_pnl_curve`` skips
                # every entry whose ``pnl_usd == 0.0`` (and a freshly-opened
                # BUY otherwise has pnl_usd == 0.0).  A BUY that is opened but
                # never closed/MTM'd in-window therefore leaked its fee out of
                # ``total_return_usd`` (return overstated by the BUY fee).
                # Record the BUY fee as *negative PnL on the BUY entry* so the
                # cumulative curve reflects the true capital change on the BUY
                # date.  If/when the position is later closed by a SELL, only
                # the SELL-leg fee is charged again — the round-trip's gross
                # PnL is independent of this entry, so there is no double count
                # (BUY fee here, SELL fee on the SELL entry).
                pnl_usd = -fee_usd
                # B5: every BUY opens its OWN FIFO lot at its own fill price.
                # Layered partial BUYs on the same condition_id therefore
                # accumulate as separate lots, each with an independent cost
                # basis, and a later SELL consumes them oldest-first.
                open_positions.setdefault(cond_id, []).append(
                    Lot(
                        shares=copy_shares,
                        fill_price=fill_price,
                        copy_usdc=copy_usdc,
                        opened_at=trade.timestamp,
                    )
                )

            entry = {
                "timestamp": trade.timestamp.isoformat(),
                "date": str(trade_date),
                "condition_id": cond_id,
                "side": side,
                "leader_price": leader_price,
                "fill_price": fill_price,
                "copy_usdc": copy_usdc,
                "copy_shares": copy_shares,
                "pnl_usd": pnl_usd,
                "fee_usd": fee_usd,
                # I8: the matching BUY's open time, stored on the SELL entry so
                # avg_hold_hours = SELL ts − BUY opened_at (was ≈0 because it
                # used SELL ts − SELL closed_at, both the SELL timestamp).
                "opened_at": opened_at.isoformat() if opened_at else None,
                "closed_at": closed_at.isoformat() if closed_at else None,
                "skipped": False,
            }
            trade_log.append(entry)

        # ---- 4. Mark-to-market open positions at period end ----------------
        # ASSUMPTION-MTM
        #
        # P1-10c (look-ahead bias): the period-end "last known price" is
        # derived FROM THE TRADE LOG ITSELF — the last in-window fill price
        # observed for each condition_id — NOT from a live get_positions()
        # snapshot taken at *run* time.  A live snapshot reflects the leader's
        # CURRENT state (potentially long after end_date) and would leak
        # future information into in-window MTM.  Building last_in_window_price
        # from trades that already passed the chronological [start,end] filter
        # guarantees no datum after end_date can influence the result.
        #
        # P1-10b (settlement snap): the derived last price is then snapped to
        # its settlement value — $1 if >= 0.95, $0 if <= 0.05, unchanged in
        # (0.05, 0.95); a voided/invalid market unwinds at entry (zero MTM).
        last_in_window_price = self._derive_last_known_prices(trade_log)
        mtm_pnl = 0.0
        for cond_id, lots in open_positions.items():
            # B5: each still-open lot is MTM'd against ITS OWN cost basis.
            # The settlement snap (a market-level resolution) and the
            # last-in-window price are shared across the condition_id, but
            # the PnL is per-lot because layered lots have different entries.
            resolved, voided = self._resolution_flags(cond_id, positions_map)
            for lot in lots:
                if lot.shares <= 0.0:
                    continue
                last_price = self._get_mtm_price(
                    cond_id,
                    {"fill_price": lot.fill_price},
                    last_in_window_price,
                    positions_map,
                )
                settle_price = compute_settlement_price(
                    last_price,
                    resolved=resolved,
                    voided=voided,
                    entry_price=lot.fill_price,
                )
                mtm = lot.shares * (settle_price - lot.fill_price)
                mtm_pnl += mtm
                trade_log.append(
                    {
                        "timestamp": datetime.combine(params.end_date, datetime.min.time())
                        .replace(tzinfo=UTC)
                        .isoformat(),
                        "date": str(params.end_date),
                        "condition_id": cond_id,
                        "side": "MTM",
                        "leader_price": lot.fill_price,
                        "fill_price": settle_price,
                        "copy_usdc": lot.copy_usdc,
                        "copy_shares": lot.shares,
                        "pnl_usd": mtm,
                        "fee_usd": 0.0,
                        "opened_at": lot.opened_at.isoformat() if lot.opened_at else None,
                        "closed_at": str(params.end_date),
                        "skipped": False,
                    }
                )

        # ---- 5. Build PnL curve --------------------------------------------
        pnl_curve = self._build_pnl_curve(
            trade_log, params.initial_capital_usd, params.start_date, params.end_date
        )

        # ---- 6. Leader PnL curve ------------------------------------------
        leader_pnl_curve = self._build_leader_pnl_curve(
            leader_daily_pnl, leader_open, positions_map, params.start_date, params.end_date
        )

        # ---- 7. Compute statistics -----------------------------------------
        stats = self._compute_statistics(pnl_curve, trade_log, params)
        total_fees_usd = float(
            sum(float(e.get("fee_usd") or 0.0) for e in trade_log if not e.get("skipped"))
        )

        result = BacktestResult(
            params=params,
            pnl_curve=pnl_curve,
            total_return_usd=stats["total_return_usd"],
            annualised_return_pct=stats["annualised_return_pct"],
            sharpe_ratio=stats["sharpe_ratio"],
            max_drawdown_pct=stats["max_drawdown_pct"],
            win_rate=stats["win_rate"],
            avg_hold_hours=stats["avg_hold_hours"],
            total_fees_usd=total_fees_usd,
            skip_stats=skip_stats,
            leader_pnl_curve=leader_pnl_curve,
            leader_total_return_usd=(
                float(leader_pnl_curve.sum()) if not leader_pnl_curve.empty else 0.0
            ),
            trade_log=trade_log,
        )

        logger.info(
            "backtest.run.done",
            extra={
                "leader": effective_leader,
                "total_return_usd": result.total_return_usd,
                "sharpe": result.sharpe_ratio,
                "skips": skip_stats.total,
            },
        )
        return result

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    # A2: defensive upper bound on cursor iterations so a misbehaving source
    # (e.g. one that ignores ``start`` and returns the same page forever)
    # can never spin the backtest into an unbounded loop.  Each datasource
    # call is itself record-bounded (LiveDataSource stops at offset >= 3000
    # per call), so 200 pages still admits ~600k trades for one window, far
    # beyond any real leader's history.
    _MAX_CURSOR_PAGES = 200

    async def _fetch_leader_trades(
        self,
        leader_address: str,
        start_date: date,
        end_date: date,
    ) -> list[ActivityRecord]:
        """Retrieve all leader TRADE records for the backtest window, time-ordered.

        A2 (PolyHermes deep-dive): the Polymarket Data API ``/activity``
        endpoint paginates by ``offset`` only, and ``LiveDataSource`` bounds
        each call at a fixed record cap (offset >= 3000) to avoid an
        unbounded sweep.  A single ``get_user_activity`` call therefore
        truncates a very active leader's history at that cap, silently
        biasing the backtest toward the most recent trades.

        Per PolyHermes' own ``DATA_API_CURSOR_PAGINATION`` note this is fixed
        with a ``start`` TIMESTAMP cursor instead of an offset: page forward
        by re-issuing the query with ``start`` advanced to the maximum
        timestamp seen so far.  Crucially the cursor is set to
        ``max(timestamp)`` WITHOUT ``+1`` second, because multiple trades can
        share the same one-second timestamp; a ``+1`` would skip every
        same-second sibling of the last record.  Re-including the boundary
        second instead means the boundary trades reappear on the next page,
        so the loop dedups by ``transactionHash`` across page boundaries.
        The ``start`` parameter already exists on the (FROZEN) datasource
        protocol, so this is implemented entirely here in the engine.

        Fixture mode is unaffected: ``FixtureDataSource`` returns the whole
        in-window slice in the first call, so the second page yields no new
        ``transactionHash`` values and the loop terminates after one
        iteration.
        """
        start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=UTC)
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=UTC)

        # Dedup keyed on the on-chain transaction hash.  The hash lives in the
        # ActivityRecord ``raw`` blob under the data-api field name
        # ``transactionHash``; ``transaction_hash`` is accepted as a defensive
        # alias.  A record with no usable hash falls back to a composite key
        # of its identifying fields so it is still dedup-able across the
        # boundary-second overlap (it would otherwise be dropped or doubled).
        seen_keys: set[str] = set()
        collected: list[ActivityRecord] = []
        cursor: datetime = start_dt
        truncated = False

        # The previous batch length, used purely to detect the terminal page:
        # the per-call record cap is a property of the datasource (it differs
        # between LiveDataSource and tests), so the engine MUST NOT hardcode
        # it.  Instead: keep paging while each page keeps yielding new,
        # cursor-advancing records; stop when a page is strictly shorter than
        # the prior page (the unambiguous "last page" signal) or yields
        # nothing new.
        prev_batch_len: int | None = None

        for _page in range(self._MAX_CURSOR_PAGES):
            batch = await self._ds.get_user_activity(
                leader_address,
                start=cursor,
                end=end_dt,
                activity_type="TRADE",
            )
            if not batch:
                break

            new_in_page = 0
            max_ts: datetime | None = None
            for rec in batch:
                key = self._activity_dedup_key(rec)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                collected.append(rec)
                new_in_page += 1
                if max_ts is None or rec.timestamp > max_ts:
                    max_ts = rec.timestamp

            # A page strictly shorter than the previous one is the terminal
            # page (the source returned fewer records than its per-call cap).
            # A page no shorter than the previous one means the cap may still
            # be jamming records, so another cursor step is required.
            page_looks_full = prev_batch_len is None or len(batch) >= prev_batch_len
            prev_batch_len = len(batch)

            # The page added nothing new: either the source ignores ``start``
            # (every record already deduped) or we genuinely reached the tail.
            # A still-full page that advanced the cursor by zero NEW records
            # means the per-call cap is jamming the cursor and the tail is
            # unreachable -> a real truncation.
            if new_in_page == 0:
                if page_looks_full:
                    truncated = True
                break

            # Advance the cursor to the newest timestamp WITHOUT +1 so
            # same-second siblings of the boundary trade are not skipped;
            # the txhash dedup above absorbs the resulting overlap.
            if max_ts is not None:
                # If the cursor cannot advance at all (every record shares the
                # window's first second) a still-full page means the tail past
                # that second is unreachable -> truncation.
                if max_ts <= cursor and page_looks_full:
                    truncated = True
                    break
                cursor = max_ts

            # A page shorter than the previous one is, by definition, the
            # last page; nothing past it remains to fetch.
            if not page_looks_full:
                break
        else:
            # Loop ran the full _MAX_CURSOR_PAGES without a natural stop.
            truncated = True

        if truncated:
            logger.warning(
                "backtest.activity_truncated",
                extra={
                    "leader": leader_address,
                    "fetched": len(collected),
                    "max_pages": self._MAX_CURSOR_PAGES,
                    "start": str(start_date),
                    "end": str(end_date),
                    "detail": (
                        "Timestamp-cursor pagination could not advance past "
                        "the datasource per-call record cap (the cursor "
                        "stalled or the page limit was exhausted). Backtest "
                        "results for this leader are based on a TRUNCATED "
                        "activity history."
                    ),
                },
            )

        # DataSource guarantees ascending timestamp order per fixture README,
        # but sort defensively: cursor pages arrive newest-first per call and
        # are concatenated across calls, so a final stable sort is required.
        return sorted(collected, key=lambda r: r.timestamp)

    @staticmethod
    def _activity_dedup_key(rec: ActivityRecord) -> str:
        """Cross-page dedup key for one activity record.

        A2: prefers the on-chain ``transactionHash`` (data-api field name;
        ``transaction_hash`` accepted as an alias) carried in ``raw``.  When
        no hash is present (e.g. fixtures, which omit it) a composite of the
        record's identifying fields is used so the boundary-second overlap
        introduced by the no-``+1`` cursor is still collapsed deterministically.
        """
        raw = rec.raw or {}
        tx = raw.get("transactionHash") or raw.get("transaction_hash")
        if tx:
            return f"tx:{tx}"
        return (
            "k:"
            f"{rec.timestamp.isoformat()}|{rec.condition_id or ''}|"
            f"{rec.side or ''}|{rec.size}|{rec.price}|{rec.usdc_size}"
        )

    async def _fetch_positions_map(self, leader_address: str) -> dict[str, PositionRecord]:
        """Return leader's open positions keyed by condition_id (for MTM)."""
        positions = await self._ds.get_positions(leader_address)
        return {p.condition_id: p for p in positions}

    @staticmethod
    def _derive_last_known_prices(
        trade_log: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Period-end last-known price per condition_id, from the trade log.

        P1-10c (look-ahead bias fix).  The engine must value still-open
        positions at the **last price observed *within* the backtest
        window**, not at a live get_positions() snapshot taken at run time
        (which reflects the leader's CURRENT — i.e. post-end_date — state and
        would leak future information).

        Every executed (non-skipped, non-MTM) trade-log entry already passed
        the chronological ``[start_date, end_date]`` filter applied in
        :meth:`_fetch_leader_trades`, and entries are appended in time order,
        so the *last* ``fill_price`` recorded for a ``condition_id`` is, by
        construction, that market's last in-window mark — provably free of
        any datum dated after ``end_date``.

        Args:
            trade_log: The trade log built during :meth:`run` (pre-MTM).

        Returns:
            ``condition_id -> last in-window fill price``.
        """
        last: dict[str, float] = {}
        for entry in trade_log:
            if entry.get("skipped") or entry.get("side") == "MTM":
                continue
            cond = entry.get("condition_id")
            fp = entry.get("fill_price")
            if cond and fp is not None:
                last[str(cond)] = float(fp)
        return last

    @staticmethod
    def _resolution_flags(
        cond_id: str,
        positions_map: dict[str, PositionRecord],
    ) -> tuple[bool | None, bool | None]:
        """Best-effort (resolved, voided) flags sourced from position ``raw``.

        Fixture data carries no resolution field, so both are ``None`` there
        and the price-band settlement rule (P1-10b) is the load-bearing path.
        For live data we read common Polymarket markers off the position's
        ``raw`` blob *only* (never from a price/value computed at run time):

        * voided/invalid  → ``raw.umaResolutionStatus == "void"`` or an
          explicit ``raw.invalid``/``raw.voided`` truthy flag.
        * resolved        → ``raw.resolved`` / ``raw.closed`` truthy, or
          ``raw.umaResolutionStatus == "resolved"``.

        Returns ``(None, None)`` when ``cond_id`` is unknown so the caller
        falls back purely to the price-band snap.
        """
        p = positions_map.get(cond_id)
        if p is None:
            return (None, None)
        raw = p.raw or {}
        uma = str(raw.get("umaResolutionStatus") or "").lower()
        voided = bool(
            raw.get("voided") or raw.get("invalid") or uma in ("void", "voided", "invalid")
        )
        resolved_flag = raw.get("resolved")
        closed_flag = raw.get("closed")
        resolved: bool | None
        if resolved_flag is not None or closed_flag is not None or uma:
            resolved = bool(resolved_flag or closed_flag or uma == "resolved")
        else:
            resolved = None
        # Only assert "not voided" when the raw blob actually carries a
        # void/invalid/uma signal; otherwise leave it unknown (None) so the
        # price-band rule alone decides.
        void_signal_present = bool("voided" in raw or "invalid" in raw or uma)
        return (resolved, voided if void_signal_present else None)

    def _get_mtm_price(
        self,
        cond_id: str,
        pos: dict[str, Any],
        last_in_window_price: dict[str, float],
        positions_map: dict[str, PositionRecord],  # noqa: ARG002 — see docstring
    ) -> float:
        """Return the look-ahead-free mark-to-market price for a position.

        P1-10c: resolution order is

        1. last in-window fill price for ``cond_id`` (derived from the trade
           log — provably no post-``end_date`` data); else
        2. the position's own entry price → ``shares * (price - entry) == 0``
           (zero MTM PnL: "no new in-window information").

        ``positions_map`` (a live get_positions() snapshot taken at run time)
        is **deliberately no longer consulted for price** — using it was the
        look-ahead bug, because it reflects the leader's CURRENT state, not
        the state as-of ``end_date``.  It is still threaded through only so
        :meth:`_resolution_flags` can read static resolution markers off
        ``raw`` (which describe whether the market resolved, not a run-time
        price); the parameter is otherwise unused here.

        This helper is called with two differently-shaped position dicts:
        copy positions use the key ``"fill_price"``; leader positions use
        ``"price"``.  Either is accepted (the prior code raised
        ``KeyError: 'fill_price'`` for leader positions).
        """
        if cond_id in last_in_window_price:
            return float(last_in_window_price[cond_id])
        # Fallback: no in-window mark → use the position's own entry price
        # so that shares * (last_price - entry_price) == 0 (zero MTM PnL).
        entry = pos.get("fill_price")
        if entry is None:
            entry = pos.get("price", 0.0)
        return float(entry)

    def _update_leader_tracking(
        self,
        leader_open: dict[str, dict[str, Any]],
        leader_daily_pnl: dict[date, float],
        trade: ActivityRecord,
        positions_map: dict[str, PositionRecord],
    ) -> None:
        """Track leader's own cost basis and realised PnL without risk gates."""
        cond_id = trade.condition_id or ""
        side = trade.side or "BUY"
        price = trade.price or 0.0
        usdc = trade.usdc_size or 0.0
        trade_date = trade.timestamp.date()

        if side == "BUY":
            leader_open[cond_id] = {
                "price": price,
                "usdc": usdc,
                "shares": usdc / price if price > 0 else 0,
            }
        elif side == "SELL" and cond_id in leader_open:
            entry = leader_open.pop(cond_id)
            pnl = entry["shares"] * (price - entry["price"])
            leader_daily_pnl[trade_date] += pnl

    def _build_leader_pnl_curve(
        self,
        leader_daily_pnl: dict[date, float],
        leader_open: dict[str, dict[str, Any]],
        positions_map: dict[str, PositionRecord],
        start_date: date,
        end_date: date,
    ) -> pd.Series:
        """Build the leader's own cumulative PnL curve (no copy overhead)."""
        idx = pd.date_range(str(start_date), str(end_date), freq="D").date
        daily: dict[date, float] = defaultdict(float, leader_daily_pnl)

        # Add MTM for leader's open positions.
        # P1-10c: a still-open leader position has, by definition, no later
        # in-window SELL, so its last in-window mark IS its own entry price
        # → zero MTM contribution (no look-ahead; the live positions_map
        # snapshot, which reflects the leader's post-end_date state, is
        # deliberately NOT used for price).  We still apply the P1-10b
        # settlement snap and honour static void/resolution markers from
        # the position raw blob.
        for cond_id, pos in leader_open.items():
            resolved, voided = self._resolution_flags(cond_id, positions_map)
            settle_price = compute_settlement_price(
                pos["price"],
                resolved=resolved,
                voided=voided,
                entry_price=pos["price"],
            )
            pnl = pos["shares"] * (settle_price - pos["price"])
            daily[end_date] += pnl

        series = pd.Series([daily.get(d, 0.0) for d in idx], index=idx, dtype=float)
        return series.cumsum()

    def _build_pnl_curve(
        self,
        trade_log: list[dict[str, Any]],
        initial_capital_usd: float,  # noqa: ARG002 — kept for API consistency
        start_date: date,
        end_date: date,
    ) -> pd.Series:
        """Build a daily cumulative PnL series from the trade log.

        Only closed/MTM entries with a non-None closed_at contribute PnL.
        The curve starts at 0.0 (PnL above initial capital).

        Args:
            trade_log:           Completed trade dicts with pnl_usd / date.
            initial_capital_usd: Kept for signature compatibility (unused).
            start_date:          Curve start date.
            end_date:            Curve end date.

        Returns:
            pd.Series indexed by date with cumulative PnL in USD.
        """
        idx = pd.date_range(str(start_date), str(end_date), freq="D").date
        daily: dict[date, float] = defaultdict(float)

        for entry in trade_log:
            if entry.get("skipped"):
                continue
            pnl = float(entry.get("pnl_usd", 0.0))
            if pnl == 0.0:
                continue
            d_str = entry.get("closed_at") or entry.get("date")
            if not d_str:
                continue
            try:
                d = date.fromisoformat(str(d_str)[:10])
            except ValueError:
                continue
            daily[d] += pnl

        series = pd.Series([daily.get(d, 0.0) for d in idx], index=idx, dtype=float)
        return series.cumsum()

    def _compute_statistics(
        self,
        pnl_curve: pd.Series,
        trade_log: list[dict[str, Any]],
        params: BacktestParams,
    ) -> dict[str, float]:
        """Compute summary statistics from the PnL curve and trade log.

        PRD §5.4 output stats: total return, annualised return, Sharpe,
        max drawdown, win rate, average holding period.

        ASSUMPTION-ANNUALIZE: uses 365 calendar days.
        ASSUMPTION-SHARPE: risk_free = 0.
        """
        if pnl_curve.empty:
            return {
                "total_return_usd": 0.0,
                "annualised_return_pct": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate": 0.0,
                "avg_hold_hours": 0.0,
            }

        total_return_usd = float(pnl_curve.iloc[-1])

        # Annualised return (ASSUMPTION-ANNUALIZE).
        # I6: the denominator MUST be the *initial capital*, not
        # ``pnl_curve.iloc[0]``.  The PnL curve starts at day-1 *cumulative
        # PnL above initial capital* — almost always 0.0 (no trade closes on
        # the very first calendar day), and even when non-zero it is a
        # meaningless, wildly volatile divisor.  Dividing by it produced
        # absurd / undefined annualised returns.  Correct form per the
        # mandate:
        #     (total_return_usd / initial_capital_usd) * (365 / n_days) * 100
        n_days = len(pnl_curve)
        capital = float(params.initial_capital_usd)
        if n_days > 0 and capital > 0.0:
            annualised_return_pct = (total_return_usd / capital) * (365.0 / n_days) * 100.0
        else:
            annualised_return_pct = 0.0

        # Daily incremental PnL for Sharpe
        daily_incremental = list(pnl_curve.diff().fillna(pnl_curve.iloc[0]))
        sharpe = compute_sharpe(daily_incremental)

        # Max drawdown
        max_dd = compute_max_drawdown(list(pnl_curve.values))

        # Win rate: fraction of *realised closed* trades with positive PnL.
        # IMP-2: a SELL is a genuine realised round-trip; an MTM entry is an
        # *unrealised* end-of-period mark.  Including MTM rows inflated the
        # denominator (every still-open position counted as a "trade"),
        # diluting / distorting win_rate.  Restrict the population to SELL
        # only — BUY entries (now carrying ``-fee_usd`` PnL, IMP-1) and MTM
        # entries are both excluded so win_rate reflects only realised
        # closing trades.
        closed = [
            e
            for e in trade_log
            if not e.get("skipped") and e.get("side") == "SELL" and e.get("pnl_usd") is not None
        ]
        if closed:
            winners = sum(1 for e in closed if float(e["pnl_usd"]) > 0.0)
            win_rate = winners / len(closed)
        else:
            win_rate = 0.0

        # Average hold hours.
        # I8: hold = SELL timestamp − the *matching BUY's* opened_at.  The
        # old code computed SELL ``timestamp`` − SELL ``closed_at``; on a
        # closing SELL both are the SELL's own timestamp, so every hold was
        # ≈0 h.  The matching BUY open time is now carried on the SELL
        # trade-log entry as ``opened_at`` (set in :meth:`run`).
        hold_hours: list[float] = []
        for entry in trade_log:
            if entry.get("skipped") or entry.get("side") in (None, "BUY", "MTM"):
                continue
            opened_str = entry.get("opened_at")
            closed_str = entry.get("closed_at") or entry.get("timestamp")
            if not opened_str or not closed_str:
                continue
            try:
                opened = datetime.fromisoformat(str(opened_str))
                closed_dt = datetime.fromisoformat(str(closed_str))
                hours = (closed_dt - opened).total_seconds() / 3600.0
                if hours >= 0:
                    hold_hours.append(hours)
            except ValueError:
                continue
        avg_hold_hours = float(np.mean(hold_hours)) if hold_hours else 0.0

        return {
            "total_return_usd": total_return_usd,
            "annualised_return_pct": annualised_return_pct,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": max_dd,
            "win_rate": win_rate,
            "avg_hold_hours": avg_hold_hours,
        }

    # Keep old private method name for backward-compat with any importers
    def _apply_slippage(self, price: float, side: str, slippage_bps: int) -> float:
        return apply_slippage(price, side, slippage_bps)

    @staticmethod
    def _skip_entry(trade: ActivityRecord, reason: str, params: BacktestParams) -> dict[str, Any]:
        return {
            "timestamp": trade.timestamp.isoformat(),
            "date": str(trade.timestamp.date()),
            "condition_id": trade.condition_id or "",
            "side": trade.side or "",
            "leader_price": trade.price,
            "fill_price": None,
            "copy_usdc": None,
            "copy_shares": None,
            "pnl_usd": 0.0,
            "fee_usd": 0.0,
            "opened_at": None,
            "closed_at": None,
            "skipped": True,
            "skip_reason": reason,
        }
