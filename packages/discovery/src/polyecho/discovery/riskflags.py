"""Risk flag detection for leader candidates.

PRD reference: §8.1 (legal/compliance), §5.2 (risk tagging).

Flags produced
--------------
insider_suspect
    Rare trades (< 5/day) that are very large (> $5K each), placed within
    24 h of market resolution, with an abnormally high win-rate (> 80 %) on
    low volume (< 50 total trades).  Any combination satisfying *all* of:
    - trades_per_day < 5
    - win_rate > 0.80 (from stats)
    - avg usdc_size > 5 000 when a valid usdc_size is available, OR
      avg size > 500 when usdc_size is absent
    - near_resolution_win_rate > 0.80 (computed here)
    - total_trades <= 50
    PRD §8.1: this MUST cause auto-reject (user_status='rejected').

strategy_drift
    30-day and 7-day strategy types differ (delegated to frequency.detect_drift).
    Stored here for completeness; caller should also insert an ``events`` row.

new_account
    Activity history shorter than 30 calendar days.  Not enough data to
    trust scores.

low_liquidity
    More than 60 % of the leader's positions are concentrated in markets
    flagged as thin (size < 100 USDC in a single position represents a
    thin market heuristic when usdc_size is unavailable).

high_concentration
    Single condition_id accounts for > 50 % of total USDC traded volume.
    Indicates the leader's record may depend on a single lucky bet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from polyecho.discovery.frequency import FrequencyStats
from polyecho.shared.datasource import ActivityRecord, PositionRecord

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_INSIDER_MAX_TRADES_PER_DAY = 5.0
_INSIDER_MIN_WIN_RATE = 0.80
_INSIDER_MIN_AVG_USDC_SIZE = 5_000.0  # USD
_INSIDER_MIN_AVG_SIZE = 500.0          # shares / tokens (fallback)
_INSIDER_NEAR_RESOLUTION_HOURS = 24
_INSIDER_NEAR_RESOLUTION_WIN_RATE = 0.80
_INSIDER_MAX_TOTAL_TRADES = 50

_NEW_ACCOUNT_MIN_DAYS = 30

_LOW_LIQUIDITY_THIN_POSITION_USD = 100.0   # USDC below this = thin market
_LOW_LIQUIDITY_CONCENTRATION = 0.60         # fraction

_HIGH_CONCENTRATION_THRESHOLD = 0.50        # fraction of volume in one market


def detect_risk_flags(
    stats: dict[str, Any],
    activity: list[ActivityRecord],
    positions: list[PositionRecord],
    *,
    frequency_stats: FrequencyStats | None = None,
    stats_7d: dict[str, Any] | None = None,
    freq_stats_7d: FrequencyStats | None = None,
) -> list[str]:
    """Detect risk flags for a candidate leader.

    PRD §8.1 — ``insider_suspect`` causes automatic rejection (enforced by the
    caller in :class:`~polyecho.discovery.worker.DiscoveryWorker`).

    Args:
        stats: Leader stats dict for the primary window (e.g. 30 d).
               Expected keys: ``pnl_usd``, ``win_rate``, ``trades_per_day``,
               ``total_trades``, ``first_trade_at`` (ISO-8601 str).
        activity: Full list of ActivityRecord objects for the evaluation window.
        positions: Current open PositionRecord list.
        frequency_stats: Pre-computed FrequencyStats for the primary window.
                         Used for drift and burstiness checks.
        stats_7d: Stats dict for the 7-day window (for drift cross-check in
                  contexts where frequency_stats is unavailable).
        freq_stats_7d: FrequencyStats for 7-day window; if provided together
                       with ``frequency_stats`` the drift flag is set here too.

    Returns:
        List of flag strings (may be empty).  Order is not significant.
    """
    flags: list[str] = []

    trades = [r for r in activity if r.activity_type == "TRADE"]

    # ── insider_suspect ───────────────────────────────────────────────────────
    # Heuristic: rare-but-large trades that close very near market resolution,
    # combined with abnormally high win-rate on a shallow history.
    #
    # Step 1: base conditions from stats
    tpd: float = float(stats.get("trades_per_day", float("inf")))
    win_rate: float = float(stats.get("win_rate", 0.0))
    total_trades: int = int(stats.get("total_trades", len(trades)))

    base_conditions_met = (
        tpd < _INSIDER_MAX_TRADES_PER_DAY
        and win_rate > _INSIDER_MIN_WIN_RATE
        and total_trades <= _INSIDER_MAX_TOTAL_TRADES
    )

    # Step 2: large average trade size
    usdc_sizes = [
        r.usdc_size for r in trades if r.usdc_size is not None and r.usdc_size > 0
    ]
    if usdc_sizes:
        avg_usdc = sum(usdc_sizes) / len(usdc_sizes)
        large_trades = avg_usdc >= _INSIDER_MIN_AVG_USDC_SIZE
    else:
        # Fallback on raw size
        sizes = [r.size for r in trades if r.size > 0]
        avg_size = sum(sizes) / len(sizes) if sizes else 0.0
        large_trades = avg_size >= _INSIDER_MIN_AVG_SIZE

    # Step 3: near-resolution win-rate
    # Identify BUY trades within 24 h of matching SELL/REDEEM close events.
    # Proxy: REDEEM records are "wins" (market resolved in favour).
    redeems = [r for r in activity if r.activity_type == "REDEEM"]
    near_res_wins = 0
    near_res_total = 0

    # Group REDEEMs by condition_id to estimate resolution time
    redeem_times: dict[str, list[datetime]] = defaultdict(list)
    for r in redeems:
        if r.condition_id:
            redeem_times[r.condition_id].append(r.timestamp)

    # R10 I3: count wins and total at the SAME granularity (per
    # CONDITION).  The OLD code added ``near_res_total += 1`` per
    # condition-with-near-res-buys but ``near_res_total += len(sells)``
    # per SELL trade — a mixed per-condition / per-trade denominator that
    # made ``near_res_wr`` meaningless and corrupted ``insider_suspect``.
    # Now: each condition that had ANY near-resolution trade activity
    # counts once toward the total; it counts as a "win" if it had a
    # near-resolution BUY (proxy: a redeemed position the wallet held
    # into resolution).  Conditions whose only near-resolution activity
    # was a SELL count toward the total but NOT as a win.
    for cid, rtimes in redeem_times.items():
        # Earliest redeem timestamp as proxy for resolution
        resolution_ts = min(rtimes)
        window_start = resolution_ts - timedelta(hours=_INSIDER_NEAR_RESOLUTION_HOURS)
        has_near_res_buy = any(
            r.condition_id == cid
            and r.side == "BUY"
            and window_start <= r.timestamp <= resolution_ts
            for r in trades
        )
        has_near_res_sell = any(
            r.condition_id == cid
            and r.side == "SELL"
            and window_start <= r.timestamp <= resolution_ts
            for r in trades
        )
        if has_near_res_buy or has_near_res_sell:
            near_res_total += 1
            if has_near_res_buy:
                near_res_wins += 1  # held into resolution & redeemed = won

    near_res_wr = (
        near_res_wins / near_res_total if near_res_total > 0 else 0.0
    )
    suspicious_near_res = (
        near_res_total > 0 and near_res_wr >= _INSIDER_NEAR_RESOLUTION_WIN_RATE
    ) or (
        # When no near-resolution data available, fall back to global win-rate
        near_res_total == 0 and win_rate >= _INSIDER_MIN_WIN_RATE and total_trades <= 10
    )

    if base_conditions_met and large_trades and suspicious_near_res:
        flags.append("insider_suspect")

    # ── strategy_drift ────────────────────────────────────────────────────────
    # Detected if both FrequencyStats windows are provided.
    if frequency_stats is not None and freq_stats_7d is not None:
        from polyecho.discovery.frequency import detect_drift  # avoid circular at module level

        if detect_drift(frequency_stats, freq_stats_7d):
            flags.append("strategy_drift")

    # ── new_account ───────────────────────────────────────────────────────────
    # History shorter than _NEW_ACCOUNT_MIN_DAYS calendar days.
    if trades:
        sorted_ts = sorted(r.timestamp for r in trades)
        history_days = (sorted_ts[-1] - sorted_ts[0]).days
        if history_days < _NEW_ACCOUNT_MIN_DAYS:
            flags.append("new_account")
    else:
        flags.append("new_account")

    # ── low_liquidity ─────────────────────────────────────────────────────────
    # > 60 % of open positions have current_value < $100.
    if positions:
        thin = sum(
            1 for p in positions if p.current_value < _LOW_LIQUIDITY_THIN_POSITION_USD
        )
        if thin / len(positions) > _LOW_LIQUIDITY_CONCENTRATION:
            flags.append("low_liquidity")

    # ── high_concentration ────────────────────────────────────────────────────
    # Single condition_id accounts for > 50 % of total USDC traded volume.
    if trades:
        vol_by_cond: dict[str, float] = defaultdict(float)
        for r in trades:
            cid = r.condition_id or "__unknown__"
            vol = r.usdc_size if r.usdc_size is not None else r.size
            vol_by_cond[cid] += vol

        total_vol = sum(vol_by_cond.values())
        if total_vol > 0:
            top_vol = max(vol_by_cond.values())
            if top_vol / total_vol > _HIGH_CONCENTRATION_THRESHOLD:
                flags.append("high_concentration")

    return flags
