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
   g. PnL for BUY trades: realised when next matching SELL on the same
      condition_id appears, or mark-to-market at end.
3. Mark-to-market all still-open positions using get_positions() current_value
   as a proxy for last known price.
4. Compute daily cumulative PnL curve + summary statistics.

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
            },
            "skip_analysis": self.skip_stats.to_dict(),
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
        # open_positions: condition_id → {size, fill_price, opened_at, side}
        open_positions: dict[str, dict[str, Any]] = {}
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
            self._update_leader_tracking(
                leader_open, leader_daily_pnl, trade, positions_map
            )

            # ---- Category whitelist filter ---------------------------------
            if params.category_whitelist is not None:
                title = str(trade.raw.get("title") or "")
                if not any(
                    cat.lower() in title.lower()
                    for cat in params.category_whitelist
                ):
                    skip_stats.category_filtered += 1
                    skip_stats.total += 1
                    trade_log.append(
                        self._skip_entry(trade, "category_filtered", params)
                    )
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
                trade_log.append(
                    self._skip_entry(trade, "insufficient_liquidity", params)
                )
                continue

            # ---- Adverse fill price (slippage + delay) ---------------------
            fill_price = apply_slippage(leader_price, side, total_slippage_bps)

            # ---- Capital check (only BUY consumes capital) -----------------
            if side == "BUY":
                if copy_usdc > capital_remaining:
                    # Partial fill not modelled; skip to keep it simple
                    skip_stats.risk_triggered += 1
                    skip_stats.total += 1
                    trade_log.append(
                        self._skip_entry(trade, "risk_insufficient_capital", params)
                    )
                    continue
                capital_remaining -= copy_usdc

            # ---- Compute copy token size (shares = usdc / fill_price) ------
            copy_shares = copy_usdc / fill_price if fill_price > 0.0 else 0.0

            # ---- PnL realisation -------------------------------------------
            pnl_usd = 0.0
            closed_at: datetime | None = None

            if side == "SELL" and cond_id in open_positions:
                # Close existing BUY position
                pos = open_positions.pop(cond_id)
                entry_price = pos["fill_price"]
                entry_shares = pos["shares"]
                # Sell at worse (lower) price → PnL = shares × (sell_price - buy_price)
                pnl_usd = min(copy_shares, entry_shares) * (fill_price - entry_price)
                capital_remaining += copy_usdc + pnl_usd
                closed_at = trade.timestamp

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
                # Open new position
                open_positions[cond_id] = {
                    "shares": copy_shares,
                    "fill_price": fill_price,
                    "opened_at": trade.timestamp,
                    "copy_usdc": copy_usdc,
                }

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
                "closed_at": closed_at.isoformat() if closed_at else None,
                "skipped": False,
            }
            trade_log.append(entry)

        # ---- 4. Mark-to-market open positions at period end ----------------
        # ASSUMPTION-MTM
        mtm_pnl = 0.0
        for cond_id, pos in open_positions.items():
            last_price = self._get_mtm_price(cond_id, pos, positions_map)
            mtm = pos["shares"] * (last_price - pos["fill_price"])
            mtm_pnl += mtm
            trade_log.append(
                {
                    "timestamp": datetime.combine(
                        params.end_date, datetime.min.time()
                    )
                    .replace(tzinfo=UTC)
                    .isoformat(),
                    "date": str(params.end_date),
                    "condition_id": cond_id,
                    "side": "MTM",
                    "leader_price": pos["fill_price"],
                    "fill_price": last_price,
                    "copy_usdc": pos["copy_usdc"],
                    "copy_shares": pos["shares"],
                    "pnl_usd": mtm,
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
        stats = self._compute_statistics(pnl_curve, trade_log)

        result = BacktestResult(
            params=params,
            pnl_curve=pnl_curve,
            total_return_usd=stats["total_return_usd"],
            annualised_return_pct=stats["annualised_return_pct"],
            sharpe_ratio=stats["sharpe_ratio"],
            max_drawdown_pct=stats["max_drawdown_pct"],
            win_rate=stats["win_rate"],
            avg_hold_hours=stats["avg_hold_hours"],
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

    async def _fetch_leader_trades(
        self,
        leader_address: str,
        start_date: date,
        end_date: date,
    ) -> list[ActivityRecord]:
        """Retrieve all leader TRADE records for the backtest window, time-ordered."""
        start_dt = datetime.combine(start_date, datetime.min.time()).replace(
            tzinfo=UTC
        )
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(
            tzinfo=UTC
        )
        records = await self._ds.get_user_activity(
            leader_address,
            start=start_dt,
            end=end_dt,
            activity_type="TRADE",
        )
        # DataSource guarantees ascending timestamp order per fixture README,
        # but sort defensively for live source compatibility.
        return sorted(records, key=lambda r: r.timestamp)

    async def _fetch_positions_map(
        self, leader_address: str
    ) -> dict[str, PositionRecord]:
        """Return leader's open positions keyed by condition_id (for MTM)."""
        positions = await self._ds.get_positions(leader_address)
        return {p.condition_id: p for p in positions}

    def _get_mtm_price(
        self,
        cond_id: str,
        pos: dict[str, Any],
        positions_map: dict[str, PositionRecord],
    ) -> float:
        """Return mark-to-market price for an open position.

        ASSUMPTION-MTM: use PositionRecord.current_value / size as last price;
        fall back to the position's own entry price (→ zero MTM PnL) when no
        position-snapshot record exists for ``cond_id``.

        This helper is called with two differently-shaped position dicts:

        * copy positions built in :meth:`run` use the key ``"fill_price"``;
        * leader positions built in :meth:`_update_leader_tracking` use the
          key ``"price"``.

        The original fallback hard-coded ``pos["fill_price"]`` and therefore
        raised ``KeyError: 'fill_price'`` for every leader position whose
        ``condition_id`` was absent from ``positions_map`` — i.e. any leader
        still holding an open position with no matching position fixture
        (reproducible with the scalper archetype).  We now accept either
        key so both call sites are safe, defaulting to 0.0 only if neither
        is present.
        """
        if cond_id in positions_map:
            p = positions_map[cond_id]
            if p.size > 0.0:
                return p.current_value / p.size
        # Fallback: no new information → use the position's own entry price
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

        # Add MTM for leader's open positions
        for cond_id, pos in leader_open.items():
            mtm_price = self._get_mtm_price(cond_id, pos, positions_map)
            pnl = pos["shares"] * (mtm_price - pos["price"])
            daily[end_date] += pnl

        series = pd.Series(
            [daily.get(d, 0.0) for d in idx], index=idx, dtype=float
        )
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

        series = pd.Series(
            [daily.get(d, 0.0) for d in idx], index=idx, dtype=float
        )
        return series.cumsum()

    def _compute_statistics(
        self,
        pnl_curve: pd.Series,
        trade_log: list[dict[str, Any]],
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

        # Annualised return (ASSUMPTION-ANNUALIZE)
        n_days = len(pnl_curve)
        if n_days > 0 and pnl_curve.iloc[0] != 0.0:
            # Use simple linear scaling from total return
            annualised_return_pct = (total_return_usd / abs(pnl_curve.iloc[0])) * (
                365.0 / n_days
            ) * 100.0
        else:
            annualised_return_pct = 0.0

        # Daily incremental PnL for Sharpe
        daily_incremental = list(pnl_curve.diff().fillna(pnl_curve.iloc[0]))
        sharpe = compute_sharpe(daily_incremental)

        # Max drawdown
        max_dd = compute_max_drawdown(list(pnl_curve.values))

        # Win rate: fraction of closed trades with positive PnL
        closed = [
            e for e in trade_log
            if not e.get("skipped") and e.get("side") not in (None, "BUY")
            and e.get("pnl_usd") is not None
        ]
        if closed:
            winners = sum(1 for e in closed if float(e["pnl_usd"]) > 0.0)
            win_rate = winners / len(closed)
        else:
            win_rate = 0.0

        # Average hold hours
        hold_hours: list[float] = []
        for entry in trade_log:
            if entry.get("skipped") or entry.get("side") in (None, "BUY", "MTM"):
                continue
            ts_str = entry.get("timestamp")
            closed_str = entry.get("closed_at")
            if not ts_str or not closed_str:
                continue
            try:
                opened = datetime.fromisoformat(str(ts_str))
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
    def _skip_entry(
        trade: ActivityRecord, reason: str, params: BacktestParams
    ) -> dict[str, Any]:
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
            "closed_at": None,
            "skipped": True,
            "skip_reason": reason,
        }
