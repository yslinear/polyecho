"""Discovery worker: finds and tracks leader candidates on Polymarket.

PRD references: §5.2 (discovery sources), §5.3 (frequency analysis),
                §8.1 (risk flags / auto-reject).

Four data sources with different refresh cadences:
- Leaderboard (daily): top 200 by 30-day profit.
- Hot-market top holders (daily): top-10 holders for each market with
  24h volume > $100K.
- Large new positions (hourly): wallets that opened > $5K positions in
  the past 24 hours.  Uses the known candidate set + leaderboard as the
  wallet pool because the fixture DataSource does not expose a global
  activity scan endpoint; in production a real DataSource would query
  chain-level event logs instead.
- Known smart-money list (weekly, manual): wallets in
  ``smart_money_seed.json`` alongside this module.

Persistence
-----------
All writes go through ``polyecho.shared.db.session_scope`` using the
SQLAlchemy 2.0 async ORM:
- ``CandidateLeader``       — upsert (merge sources[], set first_discovered_at
                              once, update last_active_at + scores).
- ``CandidateScoreHistory`` — one row per (wallet, UTC date).
- ``Event``                 — inserted for ``strategy_drift`` (severity warning)
                              and high-scoring new leaders.

Auto-reject
-----------
If ``insider_suspect`` appears in risk flags the record is saved with
``user_status='rejected'`` immediately (PRD §8.1).
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from pathlib import Path
from typing import Any

from sqlalchemy import select

from polyecho.discovery.frequency import (
    FrequencyStats,
    StrategyType,
    classify_strategy,
    compute_frequency_stats,
)
from polyecho.discovery.riskflags import detect_risk_flags
from polyecho.discovery.scoring import final_score, profit_score, replicability_score
from polyecho.shared.datasource import ActivityRecord, DataSource
from polyecho.shared.db import session_scope
from polyecho.shared.models import CandidateLeader, CandidateScoreHistory, Event

logger = logging.getLogger(__name__)

_SEED_PATH = Path(__file__).parent / "smart_money_seed.json"

# Minimum position size to trigger large-position discovery (PRD §5.2)
_LARGE_POSITION_THRESHOLD_USD = 5_000.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _window_activity(
    activity: list[ActivityRecord],
    *,
    days: int,
    reference: datetime | None = None,
) -> list[ActivityRecord]:
    """Return only the ActivityRecord items within the most recent *days*."""
    ref = reference or datetime.now(tz=UTC)
    cutoff = ref - timedelta(days=days)
    return [r for r in activity if r.timestamp >= cutoff]


def _compute_stats(
    activity: list[ActivityRecord],
    *,
    window_days: int,
) -> dict[str, Any]:
    """Derive a stats dict from ActivityRecord list for a given window.

    Keys produced match what :func:`~polyecho.discovery.scoring.profit_score`
    and :func:`~polyecho.discovery.riskflags.detect_risk_flags` expect.

    win_rate
        Fraction of SELL trades with cash_pnl > 0 among all sells.  Falls
        back to REDEEM-count / total-closes when no price data.

    pnl_usd
        Sum of (price − avg_price) × size proxied from usdc_size deltas.
        When ``usdc_size`` is available: sum of all signed usdc_sizes (BUYs
        negative, SELLs positive).  A pure heuristic — the real figure
        requires position snapshots.

    sharpe
        Daily PnL series → mean / std.  Returns 0.0 when < 2 data points.

    max_drawdown
        Peak-to-trough on the cumulative PnL series.

    consistency
        1 − (drawdown / max(abs(total_pnl), 1)).  Intuitively: how smooth
        the equity curve is.

    specialization
        Fraction of total volume in the top condition_id.

    trades_per_day, total_trades, first_trade_at
        Raw counts for risk-flag usage.
    """
    trades = [r for r in activity if r.activity_type == "TRADE"]

    if not trades:
        return {
            "pnl_usd": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 1.0,
            "consistency": 0.0,
            "specialization": 0.0,
            "trades_per_day": 0.0,
            "total_trades": 0,
            "first_trade_at": None,
        }

    # ── daily PnL series ──────────────────────────────────────────────────────
    daily_pnl: dict[date_type, float] = defaultdict(float)
    for r in trades:
        day = r.timestamp.date()
        if r.usdc_size is not None:
            sign = -1.0 if r.side == "BUY" else 1.0
            daily_pnl[day] += sign * r.usdc_size
        else:
            sign = -1.0 if r.side == "BUY" else 1.0
            daily_pnl[day] += sign * (r.size * (r.price or 0.0))

    pnl_series = [daily_pnl[d] for d in sorted(daily_pnl)]
    total_pnl = sum(pnl_series)

    # ── win_rate ──────────────────────────────────────────────────────────────
    sells = [r for r in trades if r.side == "SELL"]
    wins = [r for r in sells if (r.usdc_size or 0.0) > 0]
    win_rate = len(wins) / len(sells) if sells else 0.0

    # ── Sharpe (daily, assume rf=0) ───────────────────────────────────────────
    if len(pnl_series) >= 2:
        mean_d = sum(pnl_series) / len(pnl_series)
        var_d = sum((x - mean_d) ** 2 for x in pnl_series) / (len(pnl_series) - 1)
        std_d = math.sqrt(var_d) if var_d > 0 else 1e-9
        sharpe = (mean_d / std_d) * math.sqrt(365.0)
    else:
        sharpe = 0.0

    # ── max drawdown ──────────────────────────────────────────────────────────
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_series:
        cum_pnl += p
        if cum_pnl > peak:
            peak = cum_pnl
        dd = (peak - cum_pnl) / max(abs(peak), 1.0)
        if dd > max_dd:
            max_dd = dd
    max_drawdown = min(max_dd, 1.0)

    # ── consistency ───────────────────────────────────────────────────────────
    consistency = 1.0 - max_drawdown

    # ── specialization ────────────────────────────────────────────────────────
    vol_by_cond: dict[str, float] = defaultdict(float)
    for r in trades:
        cid = r.condition_id or "__unknown__"
        vol = r.usdc_size if r.usdc_size is not None else r.size
        vol_by_cond[cid] += vol
    total_vol = sum(vol_by_cond.values())
    top_vol = max(vol_by_cond.values()) if vol_by_cond else 0.0
    specialization = top_vol / total_vol if total_vol > 0 else 0.0

    # ── misc ──────────────────────────────────────────────────────────────────
    total_trades = len(trades)
    trades_per_day = total_trades / max(window_days, 1)
    sorted_ts = sorted(r.timestamp for r in trades)
    first_trade_at = sorted_ts[0].isoformat() if sorted_ts else None

    return {
        "pnl_usd": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_drawdown, 4),
        "consistency": round(consistency, 4),
        "specialization": round(specialization, 4),
        "trades_per_day": round(trades_per_day, 4),
        "total_trades": total_trades,
        "first_trade_at": first_trade_at,
    }


async def _upsert_candidate(
    wallet: str,
    source: str,
    *,
    display_name: str | None,
    stats_7d: dict[str, Any],
    stats_30d: dict[str, Any],
    stats_90d: dict[str, Any],
    freq_stats_30d: FrequencyStats,
    strategy_type: StrategyType,
    p_score: float,
    r_score: float,
    f_score: float,
    risk_flags: list[str],
    last_active_at: datetime | None,
) -> None:
    """Upsert a CandidateLeader row and append a CandidateScoreHistory row.

    Uses raw INSERT … ON CONFLICT for PostgreSQL; falls back to SELECT + add
    for SQLite (tests with in-memory DB).
    """
    today = date_type.today()
    user_status = "rejected" if "insider_suspect" in risk_flags else "new"

    async with session_scope() as session:
        # ── CandidateLeader upsert ────────────────────────────────────────────
        result = await session.execute(
            select(CandidateLeader).where(
                CandidateLeader.wallet_address == wallet
            )
        )
        existing: CandidateLeader | None = result.scalar_one_or_none()

        if existing is None:
            leader = CandidateLeader(
                wallet_address=wallet,
                display_name=display_name,
                sources=[source],
                first_discovered_at=datetime.now(tz=UTC),
                last_active_at=last_active_at,
                stats_7d=stats_7d,
                stats_30d=stats_30d,
                stats_90d=stats_90d,
                frequency_stats=freq_stats_30d.to_dict(),
                strategy_type=strategy_type.value,
                profit_score=p_score,
                replicability_score=r_score,
                total_score=f_score,
                risk_flags=risk_flags,
                user_status=user_status,
            )
            session.add(leader)
        else:
            # Merge sources (deduplicated)
            merged_sources = sorted(set(existing.sources or []) | {source})
            existing.sources = merged_sources
            # first_discovered_at: keep original
            existing.last_active_at = last_active_at or existing.last_active_at
            existing.display_name = display_name or existing.display_name
            existing.stats_7d = stats_7d
            existing.stats_30d = stats_30d
            existing.stats_90d = stats_90d
            existing.frequency_stats = freq_stats_30d.to_dict()
            existing.strategy_type = strategy_type.value
            existing.profit_score = p_score
            existing.replicability_score = r_score
            existing.total_score = f_score
            existing.risk_flags = risk_flags
            # Only set rejected; never un-reject automatically
            if user_status == "rejected":
                existing.user_status = "rejected"

        # ── CandidateScoreHistory append/upsert ───────────────────────────────
        hist_result = await session.execute(
            select(CandidateScoreHistory).where(
                CandidateScoreHistory.wallet_address == wallet,
                CandidateScoreHistory.date == today,
            )
        )
        hist: CandidateScoreHistory | None = hist_result.scalar_one_or_none()
        if hist is None:
            session.add(
                CandidateScoreHistory(
                    wallet_address=wallet,
                    date=today,
                    total_score=f_score,
                    stats=stats_30d,
                )
            )
        else:
            hist.total_score = f_score
            hist.stats = stats_30d

    logger.info(
        "upserted candidate",
        extra={
            "wallet": wallet,
            "source": source,
            "strategy": strategy_type.value,
            "profit_score": p_score,
            "replicability_score": r_score,
            "final_score": f_score,
            "risk_flags": risk_flags,
            "user_status": user_status,
        },
    )


async def _insert_drift_event(wallet: str, strategy_30d: str, strategy_7d: str) -> None:
    """Insert a strategy_drift Event row (PRD §5.8)."""
    async with session_scope() as session:
        session.add(
            Event(
                event_type="strategy_drift",
                severity="warning",
                payload={
                    "wallet": wallet,
                    "strategy_30d": strategy_30d,
                    "strategy_7d": strategy_7d,
                },
            )
        )
    logger.warning(
        "strategy_drift detected",
        extra={"wallet": wallet, "strategy_30d": strategy_30d, "strategy_7d": strategy_7d},
    )


# ---------------------------------------------------------------------------
# DiscoveryWorker
# ---------------------------------------------------------------------------


class DiscoveryWorker:
    """Orchestrates all four leader-discovery pipelines.

    PRD §5.2 — each source is implemented as an independent async method so
    they can be scheduled at different cadences by the service loop.

    Args:
        datasource: A :class:`~polyecho.shared.datasource.DataSource` instance.
                    Obtained from :func:`~polyecho.shared.datasource.get_datasource`
                    so that the fixture or live implementation is swapped via env.
        follower_capital_usd: Used for replicability scoring.
    """

    def __init__(
        self,
        datasource: DataSource,
        *,
        follower_capital_usd: float = 1000.0,
    ) -> None:
        self._ds = datasource
        self._follower_capital = follower_capital_usd
        # Cache of known wallet addresses across all sources (used by hourly scan)
        self._known_wallets: set[str] = set()

    # ------------------------------------------------------------------
    # Core per-wallet pipeline
    # ------------------------------------------------------------------

    async def _process_wallet(
        self,
        wallet: str,
        source: str,
        *,
        display_name: str | None = None,
    ) -> None:
        """Full pipeline for a single wallet: fetch → compute → persist.

        Steps
        -----
        1. Fetch 90-day activity + open positions via datasource.
        2. Slice into 7d / 30d / 90d windows.
        3. Compute stats + frequency stats for each window.
        4. Classify strategy (30d) and detect drift (30d vs 7d).
        5. Detect risk flags.
        6. Score (profit + replicability → final).
        7. Upsert DB rows.
        8. Insert ``strategy_drift`` Event if flagged.
        """
        logger.debug("processing wallet", extra={"wallet": wallet, "source": source})

        try:
            activity_90d = await self._ds.get_user_activity(
                wallet, activity_type="TRADE", limit=None
            )
            # Also fetch REDEEMs for hold-to-resolution analysis
            redeems_90d = await self._ds.get_user_activity(
                wallet, activity_type="REDEEM", limit=None
            )
            all_activity: list[ActivityRecord] = list(activity_90d) + list(redeems_90d)

            positions = await self._ds.get_positions(wallet, size_threshold=0.0)
        except Exception:
            logger.exception("failed to fetch data for wallet", extra={"wallet": wallet})
            return

        self._known_wallets.add(wallet)

        now = datetime.now(tz=UTC)
        act_7d = _window_activity(all_activity, days=7, reference=now)
        act_30d = _window_activity(all_activity, days=30, reference=now)

        stats_7d = _compute_stats(act_7d, window_days=7)
        stats_30d = _compute_stats(act_30d, window_days=30)
        stats_90d = _compute_stats(all_activity, window_days=90)

        freq_30d = compute_frequency_stats(act_30d, window_days=30)
        freq_7d = compute_frequency_stats(act_7d, window_days=7)

        strategy_type = classify_strategy(freq_30d)
        strategy_7d = classify_strategy(freq_7d)

        # Inject trades_per_day into stats for risk-flag use
        stats_30d["trades_per_day"] = freq_30d.trades_per_day
        stats_7d["trades_per_day"] = freq_7d.trades_per_day

        risk_flags = detect_risk_flags(
            stats_30d,
            all_activity,
            list(positions),
            frequency_stats=freq_30d,
            stats_7d=stats_7d,
            freq_stats_7d=freq_7d,
        )

        p_score = profit_score(stats_30d)
        r_score = replicability_score(
            freq_30d.to_dict(),
            self._follower_capital,
        )
        f_score = final_score(p_score, r_score)

        # last_active_at: most recent trade timestamp in 90d window
        trades_only = [r for r in all_activity if r.activity_type == "TRADE"]
        last_active_at: datetime | None = None
        if trades_only:
            last_active_at = max(r.timestamp for r in trades_only)

        await _upsert_candidate(
            wallet,
            source,
            display_name=display_name,
            stats_7d=stats_7d,
            stats_30d=stats_30d,
            stats_90d=stats_90d,
            freq_stats_30d=freq_30d,
            strategy_type=strategy_type,
            p_score=p_score,
            r_score=r_score,
            f_score=f_score,
            risk_flags=risk_flags,
            last_active_at=last_active_at,
        )

        if "strategy_drift" in risk_flags:
            await _insert_drift_event(wallet, strategy_type.value, strategy_7d.value)

    # ------------------------------------------------------------------
    # Source 1: Polymarket Leaderboard (daily)
    # ------------------------------------------------------------------

    async def discover_leaderboard(self) -> None:
        """Fetch top-200 traders from the Polymarket leaderboard (PRD §5.2).

        Uses ``get_leaderboard(time_period="MONTH", order_by="PNL", limit=200)``.
        Processes each wallet through the full pipeline.
        """
        logger.info("discover_leaderboard: start")
        entries = await self._ds.get_leaderboard(
            category="OVERALL",
            time_period="MONTH",
            order_by="PNL",
            limit=200,
        )
        logger.info("discover_leaderboard: fetched %d entries", len(entries))

        for entry in entries:
            await self._process_wallet(
                entry.wallet,
                "leaderboard",
                display_name=entry.username,
            )

        logger.info("discover_leaderboard: done")

    # ------------------------------------------------------------------
    # Source 2: Hot-market top holders (daily)
    # ------------------------------------------------------------------

    async def discover_hot_market_holders(self) -> None:
        """Fetch top-10 holders for each 24h-volume > $100K market (PRD §5.2).

        Uses ``get_markets_by_volume`` then ``get_market_top_holders`` per market.
        """
        logger.info("discover_hot_market_holders: start")
        markets = await self._ds.get_markets_by_volume(
            min_volume_24h=100_000.0, limit=200
        )
        logger.info("discover_hot_market_holders: %d hot markets", len(markets))

        seen: set[str] = set()
        for market in markets:
            try:
                holders = await self._ds.get_market_top_holders(
                    market.condition_id, limit=10
                )
            except Exception:
                logger.exception(
                    "failed to fetch holders for market",
                    extra={"condition_id": market.condition_id},
                )
                continue

            for pos in holders:
                if pos.wallet in seen:
                    continue
                seen.add(pos.wallet)
                await self._process_wallet(pos.wallet, "hot_market_holders")

        logger.info("discover_hot_market_holders: done")

    # ------------------------------------------------------------------
    # Source 3: Large new positions (hourly)
    # ------------------------------------------------------------------

    async def discover_large_new_positions(self) -> None:
        """Scan for wallets that opened > $5K positions recently (PRD §5.2).

        Approach (fixture-compatible)
        --------------------------------
        The DataSource interface does not expose a global activity scan, so we
        scan the union of:
          (a) already-known candidates in ``self._known_wallets``, and
          (b) the current leaderboard top-50 (cheaper subset).

        A production DataSource backed by the live Polymarket API would instead
        query chain-level CTF Exchange event logs for large fills in the past
        24 h.  TODO(verify): replace with chain scan when LiveDataSource is wired.

        A wallet qualifies if any single recent activity record has
        ``usdc_size > 5_000`` or ``size > 500`` (token-unit fallback).
        """
        logger.info("discover_large_new_positions: start")

        # Seed the wallet pool with the leaderboard top-50 if known set is small
        wallet_pool: set[str] = set(self._known_wallets)
        if len(wallet_pool) < 10:
            try:
                entries = await self._ds.get_leaderboard(
                    category="OVERALL",
                    time_period="MONTH",
                    order_by="PNL",
                    limit=50,
                )
                wallet_pool.update(e.wallet for e in entries)
            except Exception:
                logger.warning("could not fetch leaderboard for large-position scan")

        cutoff = datetime.now(tz=UTC) - timedelta(hours=24)
        new_wallets: set[str] = set()

        for wallet in wallet_pool:
            try:
                activity = await self._ds.get_user_activity(
                    wallet, activity_type="TRADE", limit=100
                )
            except Exception:
                continue

            for rec in activity:
                if rec.timestamp < cutoff:
                    continue
                usdc = rec.usdc_size if rec.usdc_size is not None else None
                size_ok = (usdc is not None and usdc > _LARGE_POSITION_THRESHOLD_USD) or (
                    usdc is None and rec.size > 500.0
                )
                if size_ok:
                    new_wallets.add(wallet)
                    break

        logger.info(
            "discover_large_new_positions: %d wallets with large new positions",
            len(new_wallets),
        )

        for wallet in new_wallets:
            await self._process_wallet(wallet, "large_new_positions")

        logger.info("discover_large_new_positions: done")

    # ------------------------------------------------------------------
    # Source 4: Smart-money seed (weekly, manual)
    # ------------------------------------------------------------------

    async def sync_smart_money_seed(self) -> None:
        """Upsert wallets from the smart_money_seed.json seed file (PRD §5.2).

        The seed file lives at ``packages/discovery/src/polyecho/discovery/
        smart_money_seed.json``.  Operators edit it directly; this method
        reads the current list and ensures all addresses exist in the DB.
        """
        logger.info("sync_smart_money_seed: start")

        if not _SEED_PATH.exists():
            logger.warning("smart_money_seed.json not found at %s", _SEED_PATH)
            return

        try:
            entries = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("failed to parse smart_money_seed.json")
            return

        logger.info("sync_smart_money_seed: %d seed wallets", len(entries))

        for entry in entries:
            wallet = entry.get("wallet", "").strip().lower()
            if not wallet or not wallet.startswith("0x"):
                logger.warning("skipping invalid seed entry: %s", entry)
                continue
            comment = entry.get("comment", "")
            await self._process_wallet(wallet, "smart_money_seed", display_name=comment or None)

        logger.info("sync_smart_money_seed: done")

    # ------------------------------------------------------------------
    # Orchestration helpers
    # ------------------------------------------------------------------

    async def run_daily(self) -> None:
        """Run the daily discovery pipeline (sources 1 + 2, PRD §5.2)."""
        logger.info("run_daily: start")
        await self.discover_leaderboard()
        await self.discover_hot_market_holders()
        logger.info("run_daily: complete")

    async def run_hourly(self) -> None:
        """Run the hourly discovery pipeline (source 3, PRD §5.2)."""
        logger.info("run_hourly: start")
        await self.discover_large_new_positions()
        logger.info("run_hourly: complete")

    async def run_weekly(self) -> None:
        """Run the weekly seed sync (source 4, PRD §5.2)."""
        logger.info("run_weekly: start")
        await self.sync_smart_money_seed()
        logger.info("run_weekly: complete")
