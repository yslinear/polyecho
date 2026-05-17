"""Frequency statistics and strategy classification.

PRD reference: §5.3 (frequency analysis, strategy classifier, drift detector).

Strategy taxonomy (PRD §5.3):
    Scalper          > 20 trades/day, avg hold < 1h     → NOT copyable
    Day Trader       5–20 trades/day, hold 1–8h         → marginal
    Swing Trader     1–5 trades/day, hold 1–7d          → sweet spot ✅
    Position Trader  < 1 trade/day, hold > 1 week       → copyable ✅
    Hold-to-Resolve  variable, holds until resolution   → copyable ✅
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class StrategyType(str, Enum):
    """Leader strategy classification (PRD §5.3)."""

    SCALPER = "Scalper"
    DAY_TRADER = "Day Trader"
    SWING = "Swing"
    POSITION = "Position"
    HOLD_TO_RESOLVE = "Hold-to-Resolve"
    UNKNOWN = "Unknown"


def compute_frequency_stats(
    trades: list[dict[str, Any]],
    window_days: int = 30,
) -> dict[str, Any]:
    """Compute frequency metrics from a list of raw trade records.

    PRD §5.3 — produces the ``frequency_stats`` JSONB stored in
    ``candidate_leaders``.

    Output keys match the PRD §5.3 YAML block exactly:
    - ``trades_per_day_30d`` (float)
    - ``active_days_30d`` (int)
    - ``median_gap_minutes`` (float): median gap between consecutive trades.
    - ``avg_hold_hours`` (float): average position holding time.
    - ``hold_to_resolution_pct`` (float 0–100): % of positions held to market
      resolution.
    - ``concurrent_positions_avg`` (float)
    - ``concurrent_positions_max`` (int)
    - ``burstiness`` (float 0–1): 0 = uniform gaps, 1 = highly clustered.
    - ``active_hours_utc`` (list[int]): UTC hours with ≥ 1 trade.

    Args:
        trades: List of trade dicts with at minimum ``timestamp`` (ISO-8601)
                and ``condition_id`` fields.
        window_days: Analysis window in days (default 30).

    Returns:
        Dict of frequency metrics.

    Raises:
        NotImplementedError: until Phase 1.
    """
    raise NotImplementedError("Phase 1")


def classify_strategy(frequency_stats: dict[str, Any]) -> StrategyType:
    """Classify a leader's strategy type from frequency metrics.

    PRD §5.3 strategy matrix:

    +------------------+------------------+-----------+
    | StrategyType     | trades_per_day   | avg_hold  |
    +==================+==================+===========+
    | Scalper          | > 20             | < 1h      |
    | Day Trader       | 5–20             | 1–8h      |
    | Swing            | 1–5              | 1d–7d     |
    | Position         | < 1              | > 7d      |
    | Hold-to-Resolve  | variable         | to result |
    +------------------+------------------+-----------+

    Args:
        frequency_stats: Output of :func:`compute_frequency_stats`.

    Returns:
        The most appropriate :class:`StrategyType` variant.

    Raises:
        NotImplementedError: until Phase 1.
    """
    raise NotImplementedError("Phase 1")


def detect_drift(
    stats_30d: dict[str, Any],
    stats_7d: dict[str, Any],
) -> bool:
    """Detect strategy drift by comparing 30-day vs 7-day strategy type.

    PRD §5.3 — "當 30d 與 7d 策略類型不一致，自動觸發 Web Push 警報 + 暫停跟單".

    If the classified strategy differs between the two windows, the leader is
    considered to have drifted and copy-trading must be suspended until a
    manual review.

    Args:
        stats_30d: Frequency stats computed over the last 30 days.
        stats_7d: Frequency stats computed over the last 7 days.

    Returns:
        True if drift is detected (strategies differ), False if stable.

    Raises:
        NotImplementedError: until Phase 1.
    """
    raise NotImplementedError("Phase 1")
