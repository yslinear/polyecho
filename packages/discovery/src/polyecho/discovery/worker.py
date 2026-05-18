"""Discovery worker: finds and tracks leader candidates on Polymarket.

PRD references: §5.2 (discovery sources), §5.3 (frequency analysis),
                §8.1 (risk flags / auto-reject).

Four data sources with different refresh cadences:
- Leaderboard (daily): top 200 by 30-day profit.
- Hot-market top holders (daily): top-10 holders for each market with
  24h volume > $100K.
- Large new positions (hourly): wallets that opened a > $5K position in
  the trailing 24 h window.  Scans recent ``get_user_activity`` (TRADE)
  over the known/leaderboard wallet set via the ``DataSource`` interface.
  NOTE: this gives full coverage of *already-tracked* wallets only —
  catching brand-new wallets globally needs a Polygon CTF Exchange
  event-log scan, which is deferred to Phase 4 (it shares the
  chain-listener infra and is out of scope here).
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
from polyecho.shared.events import (
    EventType,
    PushPayload,
    Severity,
    emit_event,
)
from polyecho.shared.models import CandidateLeader, CandidateScoreHistory, Event

logger = logging.getLogger(__name__)

_SEED_PATH = Path(__file__).parent / "smart_money_seed.json"

# --- High-score candidate notification threshold (PRD §5.2 / §8.1) --------
# The PRD ties the dashboard "watching" tier to a strong dual-dimension
# score; there is no hard-coded number in the PRD, so we pick a sensible
# bar on the 0–100 ``total_score`` scale (profit × replicability/100).  The
# event fires once per candidate per UPWARD crossing of this line (a state
# transition — NOT every run), so it never spams.
HIGH_SCORE_THRESHOLD: float = 70.0


async def _safe_emit_high_score(
    *,
    wallet: str,
    display_name: str | None,
    f_score: float,
    p_score: float,
    r_score: float,
) -> None:
    """Emit CANDIDATE_HIGH_SCORE (best-effort) — never crash discovery.

    Wraps the FROZEN :func:`polyecho.shared.events.emit_event` so a NOTIFY
    / DB hiccup is logged and swallowed (the discovery pipeline keeps
    running), mirroring core's ``safe_emit`` discipline.
    """
    try:
        await emit_event(
            event_type=EventType.CANDIDATE_HIGH_SCORE,
            severity=Severity.INFO,
            payload={
                "wallet": wallet,
                "display_name": display_name,
                "total_score": f_score,
                "profit_score": p_score,
                "replicability_score": r_score,
                "threshold": HIGH_SCORE_THRESHOLD,
            },
            push=PushPayload(
                title="發現高分候選領單者",
                body=(
                    f"{display_name or wallet[:10]}… 綜合評分 {f_score:.1f} "
                    f"（獲利 {p_score:.0f} × 可複製 {r_score:.0f}）"
                ),
                url=f"/discovery/{wallet}",
                tag=f"candidate_high_score:{wallet}",
                data={"wallet": wallet},
            ),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: never crash discovery
        logger.warning(
            "candidate_high_score_emit_failed_continuing",
            extra={"wallet": wallet, "error": str(exc)},
        )

# --- Large-new-position discovery thresholds (PRD §5.2) --------------------
# A wallet qualifies when a *single* recent trade has notional above this
# USD value.  PRD §5.2: "過去 24h 內 size > $5K 的新單對應錢包".
_LARGE_POSITION_THRESHOLD_USD = 5_000.0

# Token-unit fallback used only when an ActivityRecord has no ``usdc_size``
# (some fixtures / API rows omit notional).  Chosen so that, at the worst
# plausible share price of $0.01, 500 shares ≈ the $5K USD threshold; in
# practice prices are higher, so this stays conservative (never over-broad).
_LARGE_POSITION_SIZE_FALLBACK_SHARES = 500.0

# Trailing window for the hourly scan.  PRD §5.2: "過去 24h".
_LARGE_POSITION_WINDOW_HOURS = 24

# When the locally-known wallet set is smaller than this, seed the scan pool
# from the leaderboard so the hourly job is useful on a cold start.
_LARGE_POSITION_MIN_KNOWN_WALLETS = 10

# Leaderboard slice size used as the cold-start seed pool (cheaper subset).
_LARGE_POSITION_SEED_LEADERBOARD_LIMIT = 50

# I4 fix — per-wallet activity fetch for the hourly large-position scan.
#
# The previous code passed ``limit=100`` with NO pagination.  The
# Polymarket data-api ``GET /activity`` endpoint sorts by ``TIMESTAMP``
# DESC by default (NOT by trade size — verified via the Polymarket
# data-api reference: sortBy ∈ {TIMESTAMP, TOKENS, CASH}, default
# TIMESTAMP), and the live ``DataSource`` mirrors that.  So a burst
# trader who fired >100 trades in the window could have their single
# *largest* fill pushed past row 100 and silently truncated out — the
# wallet would then be missed entirely.
#
# Fix: the scan is ALREADY bounded by a 24 h trailing ``start`` window,
# so we request ``limit=None`` (the frozen ``DataSource`` protocol
# explicitly defines ``None`` as "all available within the window"; the
# live impl paginates internally via ``offset`` up to the data-api's
# 10 000-row hard cap).  Fetching the *whole* 24 h window means the
# largest fill can no longer be truncated by an arbitrary page size.
#
# Genuine truncation is now only possible if a single wallet exceeds the
# data-api's 10 000-row offset cap *within 24 h* — pathological, but we
# WARN on it (never silently drop) so the gap is observable.
_LARGE_POSITION_DATA_API_OFFSET_CAP = 10_000


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
    # P1-9b: a SELL whose ``usdc_size is None`` is *missing data*, NOT a
    # zero-value (losing) close.  The old code counted it via
    # ``(r.usdc_size or 0.0) > 0`` → False → silently a LOSS, which
    # systematically deflated win_rate (→ profit_score, 20 % weight).
    # Only SELLs with a known notional are admissible to the win/lose
    # denominator; ``usdc_size is None`` rows are excluded entirely.
    priced_sell_usdc: list[float] = [
        r.usdc_size
        for r in trades
        if r.side == "SELL" and r.usdc_size is not None
    ]
    wins = sum(1 for u in priced_sell_usdc if u > 0)
    win_rate = (
        wins / len(priced_sell_usdc) if priced_sell_usdc else 0.0
    )

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

    Concurrency (P1-9c)
    -------------------
    The prior-score read, the upward-crossing decision, the row write AND
    the CANDIDATE_HIGH_SCORE emit are all performed inside ONE transaction
    while holding a ``SELECT … FOR UPDATE`` row lock on the candidate.
    Two concurrent runs for the same wallet therefore serialise: the
    second blocks on the locked row until the first commits, then re-reads
    ``prev_score`` (now ≥ threshold) and does NOT re-emit.  The event fires
    exactly once per upward crossing.  (On SQLite ``FOR UPDATE`` is a
    no-op, which is fine — tests don't exercise true row contention.)
    """
    # P1-9a: key score-history by the UTC calendar day, NOT the server's
    # LOCAL date.  ``date.today()`` uses the process-local timezone, so a
    # non-UTC container would write history under the wrong day while every
    # other timestamp here uses ``datetime.now(tz=UTC)`` — a silent,
    # production-corrupting drift.  Use the UTC date explicitly.
    today = datetime.now(tz=UTC).date()
    user_status = "rejected" if "insider_suspect" in risk_flags else "new"

    async with session_scope() as session:
        # ── CandidateLeader upsert ────────────────────────────────────────────
        # P1-9c: FOR UPDATE locks the candidate row for the life of this
        # transaction so the read→decide→emit sequence below is atomic
        # against a concurrent same-wallet run (Postgres).  On SQLite this
        # silently degrades to a plain SELECT (acceptable for tests).
        result = await session.execute(
            select(CandidateLeader)
            .where(CandidateLeader.wallet_address == wallet)
            .with_for_update()
        )
        existing: CandidateLeader | None = result.scalar_one_or_none()

        # Capture the PRIOR score BEFORE mutation so CANDIDATE_HIGH_SCORE
        # fires once per UPWARD crossing of the threshold (a state
        # transition), not on every run while the leader stays high.
        prev_score: float | None = (
            float(existing.total_score)
            if existing is not None and existing.total_score is not None
            else None
        )
        crossed_high = f_score >= HIGH_SCORE_THRESHOLD and (
            prev_score is None or prev_score < HIGH_SCORE_THRESHOLD
        )

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

        # ── CANDIDATE_HIGH_SCORE emit — INSIDE the locked transaction ─────────
        # P1-9c: emitting here (while the FOR UPDATE row lock is still
        # held, BEFORE session_scope() commits) makes the
        # read→decide→emit sequence atomic.  A concurrent same-wallet run
        # is blocked on the row lock until this transaction commits; it
        # then re-reads ``prev_score`` (now ≥ threshold) and the upward
        # crossing is False, so the event fires EXACTLY ONCE per crossing.
        # Once per UPWARD threshold crossing only, and never for an
        # auto-rejected (insider-suspect) candidate.  ``_safe_emit_high_score``
        # is best-effort: a NOTIFY hiccup is logged & swallowed and does
        # NOT roll back the upsert (mirrors core's safe_emit discipline).
        if crossed_high and user_status != "rejected":
            await _safe_emit_high_score(
                wallet=wallet,
                display_name=display_name,
                f_score=f_score,
                p_score=p_score,
                r_score=r_score,
            )

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

    @staticmethod
    def _is_large_new_entry(rec: ActivityRecord) -> bool:
        """True if *rec* represents a single trade above the $5K threshold.

        Prefers the explicit USDC notional (``usdc_size``).  When it is
        absent — some fixtures / API rows omit it — fall back to a
        conservative share-count proxy so we do not silently drop a
        potentially large fill (PRD §5.2).
        """
        if rec.activity_type != "TRADE":
            return False
        usdc = rec.usdc_size
        if usdc is not None:
            return usdc > _LARGE_POSITION_THRESHOLD_USD
        return rec.size > _LARGE_POSITION_SIZE_FALLBACK_SHARES

    async def discover_large_new_positions(self) -> None:
        """Scan tracked wallets for > $5K trades in the trailing 24 h (PRD §5.2).

        Approach (DataSource-based, fixture-compatible)
        -----------------------------------------------
        Over the *known/leaderboard* wallet pool we call
        ``get_user_activity(..., activity_type="TRADE", start=<now-24h>)``
        and surface any wallet that has at least one entry whose notional
        exceeds ``_LARGE_POSITION_THRESHOLD_USD``.  Matches are deduped and
        tagged with the ``large_new_positions`` discovery source.

        The wallet pool is the union of:
          (a) already-known candidates in ``self._known_wallets``, and
          (b) a leaderboard slice (cold-start seed when (a) is small).

        Determinism: results are processed in sorted wallet order so the
        run is reproducible under :class:`FixtureDataSource`.

        TODO(verify): full coverage needs a Polygon CTF Exchange event-log
        scan — deferred to Phase 4 (shares the chain-listener infra).  This
        DataSource path only sees wallets already in the pool; it cannot
        discover a brand-new wallet's first large fill on its own.
        """
        logger.info("discover_large_new_positions: start")

        # Seed the wallet pool from the leaderboard if the known set is small.
        wallet_pool: set[str] = set(self._known_wallets)
        if len(wallet_pool) < _LARGE_POSITION_MIN_KNOWN_WALLETS:
            try:
                entries = await self._ds.get_leaderboard(
                    category="OVERALL",
                    time_period="MONTH",
                    order_by="PNL",
                    limit=_LARGE_POSITION_SEED_LEADERBOARD_LIMIT,
                )
                wallet_pool.update(e.wallet for e in entries)
            except Exception:
                logger.warning("could not fetch leaderboard for large-position scan")

        window_start = datetime.now(tz=UTC) - timedelta(
            hours=_LARGE_POSITION_WINDOW_HOURS
        )
        new_wallets: set[str] = set()

        for wallet in wallet_pool:
            try:
                # limit=None → fetch the FULL 24 h window (datasource
                # paginates internally).  No arbitrary page cap can
                # truncate the wallet's largest fill out of the scan.
                activity = await self._ds.get_user_activity(
                    wallet,
                    start=window_start,
                    activity_type="TRADE",
                    limit=None,
                )
            except Exception:
                logger.warning(
                    "large-position scan: activity fetch failed",
                    extra={"wallet": wallet},
                )
                continue

            # Truncation can now only happen at the data-api's hard
            # 10 000-row offset cap (pathological in a 24 h window).  If
            # we hit it, the largest fill *might* be beyond the cap — WARN
            # rather than silently drop it (I4).
            if len(activity) >= _LARGE_POSITION_DATA_API_OFFSET_CAP:
                logger.warning(
                    "large-position scan: activity hit data-api offset cap; "
                    "largest fill may be truncated",
                    extra={
                        "wallet": wallet,
                        "fetched": len(activity),
                        "cap": _LARGE_POSITION_DATA_API_OFFSET_CAP,
                    },
                )

            if any(self._is_large_new_entry(rec) for rec in activity):
                new_wallets.add(wallet)

        logger.info(
            "discover_large_new_positions: %d wallets with large new positions",
            len(new_wallets),
        )

        # Sorted for deterministic ordering under FixtureDataSource.
        for wallet in sorted(new_wallets):
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
