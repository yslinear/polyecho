"""Backtest engine: simulates copy-trading PnL.

PRD reference: §5.4 (backtest engine inputs, simulation logic, outputs).

Simulation steps (PRD §5.4):
1. Fetch all leader trades for the period from Polymarket Data API + on-chain.
2. Replay trades in chronological order:
   a. Risk check (daily loss, max position).
   b. Compute copy size.
   c. Simulate post-delay fill price with slippage.
   d. Check point-in-time liquidity.
   e. Execute or record skip reason.
3. Mark-to-market all open positions at period end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter dataclass (EXACT fields from PRD §5.4)
# ---------------------------------------------------------------------------


@dataclass
class BacktestParams:
    """Input parameters for a backtest run.

    All fields and defaults are taken verbatim from PRD §5.4.
    """

    leader_address: str
    start_date: date
    end_date: date
    initial_capital_usd: float
    copy_ratio: float  # 0.001 = follow 0.1% of leader's size
    max_position_usd: float
    min_trade_size_usd: float
    daily_loss_limit_usd: float
    slippage_bps: int = 150  # default 1.5%
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


@dataclass
class BacktestResult:
    """Output of a completed backtest run.

    PRD §5.4 output:
    - Cumulative PnL curve (daily series).
    - Key statistics.
    - Skip analysis.
    - Comparison against leader's raw PnL.
    """

    params: BacktestParams

    # Cumulative PnL curve — index is date, values are USD PnL
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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class BacktestEngine:
    """Executes copy-trade backtests using historical Polymarket data.

    PRD §5.4.

    Usage::

        params = BacktestParams(
            leader_address="0x...",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 3, 31),
            initial_capital_usd=1000.0,
            copy_ratio=0.001,
            max_position_usd=100.0,
            min_trade_size_usd=5.0,
            daily_loss_limit_usd=50.0,
        )
        engine = BacktestEngine()
        result = await engine.run(params)
    """

    async def run(self, params: BacktestParams) -> BacktestResult:
        """Execute a full backtest and return aggregated results.

        PRD §5.4 simulation steps:
        1. Load leader trades for the period (Polymarket Data API + on-chain).
        2. Replay chronologically:
           - Risk check (daily loss limit, max position size).
           - Compute copy size = leader.size × params.copy_ratio.
           - Apply execution_delay_seconds offset to determine fill time.
           - Apply slippage_bps to the price at fill time.
           - Check point-in-time liquidity; skip if insufficient.
           - Skip if copy size < min_trade_size_usd.
        3. Mark-to-market all open positions at end_date.
        4. Build PnL curve, compute statistics, collect skip reasons.

        Args:
            params: Fully populated :class:`BacktestParams`.

        Returns:
            :class:`BacktestResult` with PnL curve, stats, and skip analysis.

        Raises:
            NotImplementedError: until Phase 2.
        """
        raise NotImplementedError("Phase 2")

    async def _fetch_leader_trades(
        self,
        leader_address: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """Retrieve all leader trades for the backtest window.

        Combines Polymarket Data API results with on-chain event logs to
        ensure completeness.

        # TODO(verify): confirm Data API pagination and on-chain CTF event
        # log structure with py-clob-client-v2.

        Args:
            leader_address: Wallet address of the leader.
            start_date: Inclusive start date.
            end_date: Inclusive end date.

        Returns:
            Sorted list of trade dicts (ascending timestamp).

        Raises:
            NotImplementedError: until Phase 2.
        """
        raise NotImplementedError("Phase 2")

    def _apply_slippage(
        self,
        price: float,
        side: str,
        slippage_bps: int,
    ) -> float:
        """Compute the simulated fill price after slippage.

        PRD §5.4 — slippage is applied symmetrically: BUY gets a worse
        (higher) price, SELL gets a worse (lower) price.

        Args:
            price: Mid-market price at fill time (0–1 for binary markets).
            side: "BUY" or "SELL".
            slippage_bps: Slippage in basis points (1 bps = 0.01%).

        Returns:
            Adjusted fill price clamped to [0.0, 1.0].
        """
        factor = slippage_bps / 10_000.0
        if side == "BUY":
            return min(1.0, price * (1 + factor))
        return max(0.0, price * (1 - factor))

    def _build_pnl_curve(
        self,
        trade_log: list[dict[str, Any]],
        initial_capital_usd: float,
        start_date: date,
        end_date: date,
    ) -> pd.Series:
        """Build a daily cumulative PnL series from the trade log.

        Args:
            trade_log: Completed trade dicts with ``pnl_usd`` and
                       ``closed_at`` fields.
            initial_capital_usd: Starting capital (base of 0% return).
            start_date: Curve start date.
            end_date: Curve end date.

        Returns:
            pd.Series indexed by date with cumulative PnL in USD.

        Raises:
            NotImplementedError: until Phase 2.
        """
        raise NotImplementedError("Phase 2")

    def _compute_statistics(
        self,
        pnl_curve: pd.Series,
        trade_log: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Compute summary statistics from the PnL curve and trade log.

        PRD §5.4 output stats: total return, annualised return, Sharpe,
        max drawdown, win rate, average holding period.

        Args:
            pnl_curve: Daily cumulative PnL (output of :meth:`_build_pnl_curve`).
            trade_log: Full trade log.

        Returns:
            Dict with keys matching :class:`BacktestResult` fields.

        Raises:
            NotImplementedError: until Phase 2.
        """
        raise NotImplementedError("Phase 2")
