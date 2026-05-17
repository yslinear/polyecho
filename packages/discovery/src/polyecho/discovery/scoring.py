"""Leader scoring model.

PRD reference: §5.2 (dual-dimension scoring).

Final score formula:
    final_score = profit_score × (replicability_score / 100)

Multiplicative (not additive): if replicability is 0, the leader cannot be
copied regardless of profit performance.

profit_score weights (PRD §5.2):
    PnL          25%
    Win-rate     20%
    Sharpe       20%
    Max drawdown 15%
    Consistency  10%
    Specialization 10%

replicability_score factors (PRD §5.2):
    Frequency match   × capital match × burstiness × time-overlap
"""

from __future__ import annotations

from typing import Any


def profit_score(stats: dict[str, Any]) -> float:
    """Compute the profit dimension score (0–100).

    PRD §5.2 — weighted combination of PnL, win-rate, Sharpe ratio, max
    drawdown, consistency, and market specialization.

    Args:
        stats: Leader stats dict, typically ``stats_30d`` JSONB from
               ``candidate_leaders``.  Expected keys (all optional, defaults
               to 0 if missing):
               - ``pnl_usd`` (float): realised PnL over the period.
               - ``win_rate`` (float 0–1): fraction of winning trades.
               - ``sharpe`` (float): Sharpe ratio.
               - ``max_drawdown`` (float 0–1): maximum drawdown fraction.
               - ``consistency`` (float 0–1): intra-period PnL smoothness.
               - ``specialization`` (float 0–1): concentration in top category.

    Returns:
        Score in [0, 100].

    Raises:
        NotImplementedError: until Phase 1.
    """
    raise NotImplementedError("Phase 1")


def replicability_score(
    frequency_stats: dict[str, Any],
    follower_capital_usd: float,
) -> float:
    """Compute the replicability dimension score (0–100).

    PRD §5.2 — multiplicative combination of:
    - Frequency match: can the follower react within the leader's typical gap?
    - Capital match: is the follower's capital proportional enough to copy?
    - Burstiness: low burstiness is more predictable (inverse penalty).
    - Time-overlap: fraction of leader's active hours that overlap with
      follower's monitoring window.

    Args:
        frequency_stats: Frequency metrics dict from ``candidate_leaders``.
                         Expected keys from PRD §5.3 YAML block:
                         ``trades_per_day_30d``, ``median_gap_minutes``,
                         ``avg_hold_hours``, ``burstiness``,
                         ``active_hours_utc``, ``concurrent_positions_avg``.
        follower_capital_usd: Follower's available trading capital in USD.

    Returns:
        Score in [0, 100].

    Raises:
        NotImplementedError: until Phase 1.
    """
    raise NotImplementedError("Phase 1")


def final_score(
    p_score: float,
    r_score: float,
) -> float:
    """Combine profit and replicability scores into the final ranking score.

    PRD §5.2:
        final_score = profit_score × (replicability_score / 100)

    The multiplicative formula ensures a leader with replicability = 0 scores
    zero regardless of profit, because "跟不到就是 0".

    Args:
        p_score: Result of :func:`profit_score`, range [0, 100].
        r_score: Result of :func:`replicability_score`, range [0, 100].

    Returns:
        Final score in [0, 100].
    """
    return p_score * (r_score / 100.0)
