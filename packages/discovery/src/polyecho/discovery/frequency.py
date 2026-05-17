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

import statistics
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import numpy as np

from polyecho.shared.datasource import ActivityRecord


class StrategyType(str, Enum):
    """Leader strategy classification (PRD §5.3)."""

    SCALPER = "Scalper"
    DAY_TRADER = "Day Trader"
    SWING = "Swing"
    POSITION = "Position"
    HOLD_TO_RESOLVE = "Hold-to-Resolve"
    UNKNOWN = "Unknown"


class FrequencyStats:
    """Container for PRD §5.3 frequency metrics.

    All fields match the §5.3 YAML block verbatim so they can be
    serialised directly to ``candidate_leaders.frequency_stats`` JSONB.
    """

    __slots__ = (
        "trades_per_day",
        "active_days",
        "median_gap_minutes",
        "avg_hold_hours",
        "hold_to_resolution_pct",
        "concurrent_positions_avg",
        "concurrent_positions_max",
        "burstiness",
        "active_hours_utc",
    )

    def __init__(
        self,
        *,
        trades_per_day: float,
        active_days: int,
        median_gap_minutes: float,
        avg_hold_hours: float,
        hold_to_resolution_pct: float,
        concurrent_positions_avg: float,
        concurrent_positions_max: int,
        burstiness: float,
        active_hours_utc: list[int],
    ) -> None:
        self.trades_per_day = trades_per_day
        self.active_days = active_days
        self.median_gap_minutes = median_gap_minutes
        self.avg_hold_hours = avg_hold_hours
        self.hold_to_resolution_pct = hold_to_resolution_pct
        self.concurrent_positions_avg = concurrent_positions_avg
        self.concurrent_positions_max = concurrent_positions_max
        self.burstiness = burstiness
        self.active_hours_utc = active_hours_utc

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSONB storage."""
        return {
            "trades_per_day": self.trades_per_day,
            "active_days": self.active_days,
            "median_gap_minutes": self.median_gap_minutes,
            "avg_hold_hours": self.avg_hold_hours,
            "hold_to_resolution_pct": self.hold_to_resolution_pct,
            "concurrent_positions_avg": self.concurrent_positions_avg,
            "concurrent_positions_max": self.concurrent_positions_max,
            "burstiness": self.burstiness,
            "active_hours_utc": self.active_hours_utc,
        }


def compute_frequency_stats(
    activity: list[ActivityRecord],
    *,
    window_days: int,
) -> FrequencyStats:
    """Compute frequency metrics from a list of ActivityRecord objects.

    PRD §5.3 — produces all keys in the ``frequency_stats`` JSONB block.

    Algorithm sketch
    ----------------
    trades_per_day
        Total TRADE records divided by the window length.

    active_days
        Count of distinct UTC calendar days that contain at least one trade.

    median_gap_minutes
        Gaps between consecutive trade timestamps (sorted ascending).
        Computed as the median of these gaps.  Returns 0.0 if < 2 trades.

    avg_hold_hours
        For each condition_id, pair the earliest BUY with the next SELL (or
        REDEEM) to estimate hold duration.  Average across all pairs.
        Falls back to 0.0 when no closed pairs exist.

    hold_to_resolution_pct
        Fraction of REDEEM records (which indicate a market resolved while the
        position was held) relative to all position-closing events (SELLs +
        REDEEMs).  0–100.

    concurrent_positions_avg / max
        Simulate an open-positions counter: +1 on BUY, −1 on SELL/REDEEM.
        Sample at each event boundary.

    burstiness
        Coefficient of variation of inter-trade gaps: CV = std / mean.
        Clipped to [0, 1].  0 = perfectly uniform, 1 = highly clustered.
        Reference: Goh & Barabási (2008) burstiness parameter.

    active_hours_utc
        Sorted list of UTC hour integers (0–23) in which at least one trade
        occurred.

    Args:
        activity: List of ActivityRecord (may include REDEEMs, all types).
        window_days: Length of the analysis window in calendar days.

    Returns:
        :class:`FrequencyStats` instance.
    """
    trades = [r for r in activity if r.activity_type == "TRADE"]
    if not trades:
        return FrequencyStats(
            trades_per_day=0.0,
            active_days=0,
            median_gap_minutes=0.0,
            avg_hold_hours=0.0,
            hold_to_resolution_pct=0.0,
            concurrent_positions_avg=0.0,
            concurrent_positions_max=0,
            burstiness=0.0,
            active_hours_utc=[],
        )

    # Sort ascending by timestamp
    trades_sorted = sorted(trades, key=lambda r: r.timestamp)
    all_sorted = sorted(activity, key=lambda r: r.timestamp)

    # ── trades_per_day ────────────────────────────────────────────────────────
    trades_per_day = len(trades) / max(window_days, 1)

    # ── active_days ───────────────────────────────────────────────────────────
    active_days = len({r.timestamp.date() for r in trades_sorted})

    # ── inter-trade gaps & burstiness ─────────────────────────────────────────
    timestamps_s = [r.timestamp.timestamp() for r in trades_sorted]
    gaps_s: list[float] = []
    for i in range(1, len(timestamps_s)):
        g = timestamps_s[i] - timestamps_s[i - 1]
        if g > 0:
            gaps_s.append(g)

    gaps_min = [g / 60.0 for g in gaps_s]
    median_gap_minutes = float(statistics.median(gaps_min)) if gaps_min else 0.0

    if len(gaps_s) >= 2:
        arr = np.array(gaps_s, dtype=float)
        mean_g = float(np.mean(arr))
        std_g = float(np.std(arr, ddof=1))
        cv = (std_g / mean_g) if mean_g > 0 else 0.0
        burstiness = float(np.clip(cv, 0.0, 1.0))
    else:
        burstiness = 0.0

    # ── avg_hold_hours & hold_to_resolution_pct ───────────────────────────────
    # Group events by condition_id to pair opens (BUY) with closes (SELL/REDEEM)
    by_condition: dict[str, list[ActivityRecord]] = defaultdict(list)
    for r in all_sorted:
        if r.condition_id:
            by_condition[r.condition_id].append(r)

    hold_durations_h: list[float] = []
    resolution_closes = 0
    total_closes = 0

    for cond_records in by_condition.values():
        sorted_recs = sorted(cond_records, key=lambda r: r.timestamp)
        open_time: datetime | None = None
        for rec in sorted_recs:
            if rec.activity_type == "TRADE" and rec.side == "BUY":
                if open_time is None:
                    open_time = rec.timestamp
            elif rec.activity_type == "TRADE" and rec.side == "SELL":
                total_closes += 1
                if open_time is not None:
                    delta_h = (rec.timestamp - open_time).total_seconds() / 3600.0
                    hold_durations_h.append(max(delta_h, 0.0))
                    open_time = None
            elif rec.activity_type == "REDEEM":
                total_closes += 1
                resolution_closes += 1
                if open_time is not None:
                    delta_h = (rec.timestamp - open_time).total_seconds() / 3600.0
                    hold_durations_h.append(max(delta_h, 0.0))
                    open_time = None

    avg_hold_hours = float(statistics.mean(hold_durations_h)) if hold_durations_h else 0.0
    hold_to_resolution_pct = (
        100.0 * resolution_closes / total_closes if total_closes > 0 else 0.0
    )

    # ── concurrent_positions ─────────────────────────────────────────────────
    # Simulate open-position counter using all events (BUY +1, SELL/REDEEM −1)
    counter = 0
    samples: list[int] = [0]
    for rec in all_sorted:
        if rec.activity_type == "TRADE" and rec.side == "BUY":
            counter += 1
        elif rec.activity_type in ("TRADE", "REDEEM") and (
            rec.side == "SELL" or rec.activity_type == "REDEEM"
        ):
            counter = max(counter - 1, 0)
        samples.append(counter)

    concurrent_positions_avg = float(statistics.mean(samples)) if samples else 0.0
    concurrent_positions_max = int(max(samples)) if samples else 0

    # ── active_hours_utc ─────────────────────────────────────────────────────
    active_hours_utc = sorted({r.timestamp.hour for r in trades_sorted})

    return FrequencyStats(
        trades_per_day=round(trades_per_day, 4),
        active_days=active_days,
        median_gap_minutes=round(median_gap_minutes, 2),
        avg_hold_hours=round(avg_hold_hours, 4),
        hold_to_resolution_pct=round(hold_to_resolution_pct, 2),
        concurrent_positions_avg=round(concurrent_positions_avg, 4),
        concurrent_positions_max=concurrent_positions_max,
        burstiness=round(burstiness, 4),
        active_hours_utc=active_hours_utc,
    )


def classify_strategy(stats: FrequencyStats) -> StrategyType:
    """Classify a leader's strategy type from FrequencyStats.

    PRD §5.3 strategy matrix with thresholds:

    +------------------+------------------+---------------------+
    | StrategyType     | trades_per_day   | avg_hold            |
    +==================+==================+=====================+
    | Scalper          | > 20             | < 1 h               |
    | Day Trader       | 5–20             | 1–8 h               |
    | Swing            | 1–5              | 1 d – 7 d           |
    | Position         | < 1              | > 7 d               |
    | Hold-to-Resolve  | any              | hold_to_res >= 60 % |
    +------------------+------------------+---------------------+

    Hold-to-Resolve takes priority when >= 60 % of closing events are
    redemptions, since that pattern supersedes the trades_per_day count.

    Args:
        stats: :class:`FrequencyStats` as returned by
               :func:`compute_frequency_stats`.

    Returns:
        The most appropriate :class:`StrategyType` variant.
    """
    tpd = stats.trades_per_day
    hold_h = stats.avg_hold_hours
    hold_d = hold_h / 24.0

    # Hold-to-Resolve: majority of closes are redemptions
    if stats.hold_to_resolution_pct >= 60.0:
        return StrategyType.HOLD_TO_RESOLVE

    # Scalper: > 20/day AND hold < 1 h
    if tpd > 20.0 and hold_h < 1.0:
        return StrategyType.SCALPER

    # Day Trader: 5–20/day AND hold 1–8 h
    if 5.0 <= tpd <= 20.0 and 1.0 <= hold_h <= 8.0:
        return StrategyType.DAY_TRADER

    # Swing sweet-spot: 1–5/day AND hold 1 d – 7 d
    if 1.0 <= tpd <= 5.0 and 1.0 <= hold_d <= 7.0:
        return StrategyType.SWING

    # Position: < 1/day AND hold > 7 d
    if tpd < 1.0 and hold_d > 7.0:
        return StrategyType.POSITION

    # Fallback heuristics for edge cases
    # High frequency but longer hold → lean toward Day Trader
    if tpd > 5.0:
        return StrategyType.DAY_TRADER
    if hold_d > 1.0:
        return StrategyType.SWING

    return StrategyType.UNKNOWN


def detect_drift(
    stats_30d: FrequencyStats,
    stats_7d: FrequencyStats,
) -> bool:
    """Detect strategy drift by comparing 30-day vs 7-day strategy type.

    PRD §5.3 — "當 30d 與 7d 策略類型不一致，自動觸發 Web Push 警報 + 暫停跟單".

    If the classified strategy differs between the two windows the leader is
    considered to have drifted and copy-trading must be suspended until a
    manual review.

    Args:
        stats_30d: FrequencyStats computed over the last 30 days.
        stats_7d: FrequencyStats computed over the last 7 days.

    Returns:
        True if drift is detected (strategies differ), False if stable.
    """
    type_30d = classify_strategy(stats_30d)
    type_7d = classify_strategy(stats_7d)
    return type_30d != type_7d
