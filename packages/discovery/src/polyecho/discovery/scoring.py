"""Leader scoring model.

PRD reference: §5.2 (dual-dimension scoring).

Final score formula:
    final_score = profit_score × (replicability_score / 100)

Multiplicative (not additive): if replicability is 0, the leader cannot be
copied regardless of profit performance — "跟不到就是 0".

profit_score weights (PRD §5.2):
    PnL          25%
    Win-rate     20%
    Sharpe       20%
    Max drawdown 15%
    Consistency  10%
    Specialization 10%

replicability_score factors (PRD §5.2):
    Frequency match × capital match × burstiness penalty × time-overlap

Normalisation decisions (documented per sub-metric):
    pnl_usd       → sigmoid-like: score100 = 100/(1+exp(-pnl/10000))
                    A $10K PnL ≈ 50 pts; $50K ≈ 99 pts; negative → <50.
    win_rate      → linear: score100 = win_rate * 100  (0.65 → 65 pts)
    sharpe        → clamp: score100 = clamp(sharpe*25, 0, 100)
                    Sharpe 2.0 → 50 pts; 4.0 → 100 pts.
    max_drawdown  → inverted linear: score100 = (1 - drawdown) * 100
                    0 % drawdown → 100 pts; 50 % drawdown → 50 pts.
    consistency   → direct: score100 = consistency * 100
    specialization→ direct: score100 = specialization * 100
"""

from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------------------
# Follower waking-hours reference (Asia/Taipei, UTC+8)
# Assumed monitoring window: 08:00–23:00 Taipei = 00:00–15:00 UTC
# ---------------------------------------------------------------------------
_FOLLOWER_ACTIVE_HOURS_UTC: frozenset[int] = frozenset(range(0, 15))  # 00–14 UTC


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def _sigmoid(x: float) -> float:
    """Numerically-stable logistic sigmoid in (0, 1).

    The naive form ``1 / (1 + exp(-x))`` raises :class:`OverflowError` for
    large negative ``x`` because ``exp(-x)`` overflows ``float``.  A leader
    with a catastrophic loss (e.g. ``pnl_usd = -1e9``) is entirely realistic
    and must *saturate to 0*, never crash the discovery pipeline.  We branch
    on the sign so the exponent argument is always <= 0 and therefore bounded
    in ``(0, 1]``.
    """
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def profit_score(stats: dict[str, Any]) -> float:
    """Compute the profit dimension score (0–100).

    PRD §5.2 — weighted combination of PnL, win-rate, Sharpe ratio, max
    drawdown, consistency, and market specialization.

    Sub-metric normalisation (see module docstring for rationale):
    - pnl_usd      → sigmoid: 100 / (1 + exp(-pnl / 10_000))
    - win_rate     → linear 0–1 → 0–100
    - sharpe       → clamp(sharpe × 25, 0, 100) [Sharpe 4 → 100]
    - max_drawdown → (1 - drawdown) × 100, drawdown in [0, 1]
    - consistency  → direct × 100, in [0, 1]
    - specialization → direct × 100, in [0, 1]

    Weights per PRD §5.2:
        PnL 25 % + win_rate 20 % + Sharpe 20 % + max_drawdown 15 %
        + consistency 10 % + specialization 10 %

    Args:
        stats: Leader stats dict (typically ``stats_30d`` JSONB).
               All keys are optional and default to neutral values when missing.

    Returns:
        Score in [0, 100].
    """
    # ── sub-metric extraction with sensible defaults ──────────────────────────
    pnl_usd: float = float(stats.get("pnl_usd", 0.0))
    win_rate: float = float(stats.get("win_rate", 0.0))      # 0–1
    sharpe: float = float(stats.get("sharpe", 0.0))
    max_drawdown: float = float(stats.get("max_drawdown", 1.0))  # 0–1
    consistency: float = float(stats.get("consistency", 0.0))   # 0–1
    specialization: float = float(stats.get("specialization", 0.0))  # 0–1

    # ── normalise each sub-metric to [0, 100] ─────────────────────────────────
    # PnL: sigmoid centred at 0 with scale $10K.
    # $0 → 50 pts; $10K → ~73 pts; $50K → ~99 pts; −$10K → ~27 pts.
    # Numerically stable: huge negative PnL saturates to 0 instead of
    # raising OverflowError (see :func:`_sigmoid`).
    pnl_norm = 100.0 * _sigmoid(pnl_usd / 10_000.0)

    # Win-rate: linear. 0.5 → 50 pts; 0.65 → 65 pts; 1.0 → 100 pts.
    win_norm = _clamp(win_rate * 100.0, 0.0, 100.0)

    # Sharpe: scale by 25 so Sharpe 2.0 → 50 pts, 4.0 → 100 pts.
    sharpe_norm = _clamp(sharpe * 25.0, 0.0, 100.0)

    # Max drawdown: 0 % → 100 pts; 50 % → 50 pts; 100 % → 0 pts.
    drawdown_norm = _clamp((1.0 - max_drawdown) * 100.0, 0.0, 100.0)

    # Consistency and specialization: direct mapping 0–1 → 0–100.
    consistency_norm = _clamp(consistency * 100.0, 0.0, 100.0)
    specialization_norm = _clamp(specialization * 100.0, 0.0, 100.0)

    # ── weighted sum (weights sum to 1.0) ────────────────────────────────────
    score = (
        pnl_norm * 0.25
        + win_norm * 0.20
        + sharpe_norm * 0.20
        + drawdown_norm * 0.15
        + consistency_norm * 0.10
        + specialization_norm * 0.10
    )

    return round(_clamp(score, 0.0, 100.0), 4)


def replicability_score(
    frequency_stats: dict[str, Any],
    follower_capital_usd: float = 1000.0,
) -> float:
    """Compute the replicability dimension score (0–100).

    PRD §5.2 — multiplicative product of four factors (each 0–1) × 100.
    "跟不到就是 0" — a single zero factor zeros the entire score.

    Factor definitions
    ------------------
    1. frequency_match
       Can the follower react within the leader's typical gap?
       Followers need ≥ 30 s to detect + submit an order.
       freq_match = clamp(median_gap / 30 min, 0, 1)
       (≥ 30 min gap → 1.0; < 30 min → proportionally lower)

    2. capital_match
       Is the follower's capital large enough to copy at minimum trade size?
       We assume a minimum trade size of $10 USDC.
       Typical leader trade size estimated as:
           est_trade_usd = follower_capital / concurrent_positions_avg
       capital_match = clamp(est_trade_usd / 10, 0, 1)

    3. burstiness_factor (penalty)
       Highly bursty leaders are harder to copy because orders arrive in
       rapid clusters.  ``burstiness`` here is the clipped coefficient of
       variation of inter-trade gaps in ``[0, 1]`` (see
       :func:`polyecho.discovery.frequency.compute_frequency_stats` — I7;
       it is NOT the Goh-Barabási ``B ∈ [−1, 1]`` index).  The factor is
       ``clip(1 − burstiness, 0, 1)``; the ``[0, 1]`` domain is exactly
       what keeps this a well-defined penalty (a raw Goh-Barabási value
       would push ``1 − B`` outside ``[0, 1]`` for regular traders).

    4. time_overlap
       Fraction of the leader's active UTC hours that overlap with the
       follower's assumed monitoring window (00:00–14:59 UTC, i.e.
       08:00–22:59 Taipei time).
       time_overlap = |leader_hours ∩ follower_hours| / max(|leader_hours|, 1)

    Args:
        frequency_stats: Frequency metrics dict from ``candidate_leaders``.
        follower_capital_usd: Follower's available trading capital in USD.

    Returns:
        Score in [0, 100].
    """
    median_gap_min: float = float(frequency_stats.get("median_gap_minutes", 0.0))
    burstiness: float = float(frequency_stats.get("burstiness", 1.0))
    active_hours: list[int] = list(frequency_stats.get("active_hours_utc", []))
    concurrent_avg: float = float(
        frequency_stats.get("concurrent_positions_avg", 1.0)
    )
    concurrent_avg = max(concurrent_avg, 1.0)

    # 1. Frequency match: gap ≥ 30 min → 1.0; < 30 min → proportional.
    frequency_match = _clamp(median_gap_min / 30.0, 0.0, 1.0)

    # 2. Capital match: follower can copy if estimated trade size ≥ $10.
    est_trade_usd = follower_capital_usd / concurrent_avg
    capital_match = _clamp(est_trade_usd / 10.0, 0.0, 1.0)

    # 3. Burstiness penalty: 0 burstiness → 1.0 factor; 1.0 burstiness → 0.0.
    burstiness_factor = _clamp(1.0 - burstiness, 0.0, 1.0)

    # 4. Time-overlap with follower's Asia/Taipei waking hours.
    # R10 I1: ``active_hours`` is a SET of distinct UTC hour-of-day
    # buckets conceptually, but a caller may pass a non-deduplicated
    # list (e.g. one entry per active day).  Using the raw ``len()`` as
    # the denominator inflated it and deflated the score.  Deduplicate
    # both the numerator set and the denominator so the ratio is the
    # true fraction of distinct active hours that overlap.
    active_hours_set = set(active_hours)
    if active_hours_set:
        overlap = len(active_hours_set & _FOLLOWER_ACTIVE_HOURS_UTC)
        time_overlap = overlap / len(active_hours_set)
    else:
        time_overlap = 0.0

    score = (
        frequency_match
        * capital_match
        * burstiness_factor
        * time_overlap
        * 100.0
    )

    return round(_clamp(score, 0.0, 100.0), 4)


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
    return round(_clamp(p_score * (r_score / 100.0), 0.0, 100.0), 4)
