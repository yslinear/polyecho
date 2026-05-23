"""Discovery worker: finds and tracks leader candidates on Polymarket.

PRD references: §5.2 (discovery sources), §5.3 (frequency analysis),
                §8.1 (risk flags / auto-reject).

Three data sources with different refresh cadences:
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

All discovered wallets come from REAL on-chain / Data-API activity via
the ``DataSource``; the worker never seeds itself from a bundled wallet
list (no synthetic/fixture wallets are ever written to the operator DB).

Persistence
-----------
All writes go through ``polyecho.shared.db.session_scope`` using the
SQLAlchemy 2.0 async ORM:
- ``CandidateLeader``       — upsert (merge sources[], set first_discovered_at
                              once, update last_active_at + scores).
- ``CandidateScoreHistory`` — one row per (wallet, UTC date).
- ``Event``                 — inserted for ``strategy_drift`` (severity warning)
                              and high-scoring new leaders.

Auto-reject (money-safety)
--------------------------
If ``insider_suspect`` appears in risk flags the record is saved with
``lifecycle_state='retired'`` immediately (PRD §8.1). ``retired`` is the
C1 ladder terminal state (``TERMINAL_STATES`` in
:mod:`polyecho.paper.state_machine`), and the state machine's
:func:`advance` short-circuits a retired candidate with
``reason="terminal_state"`` and ``changed=False`` — so an insider_suspect
can never be auto-bootstrapped into the paper ladder. This replaces the
prior ``user_status='rejected'`` exclusion mechanism (the column was
removed from the schema along with this rewire); the exclusion semantic
is preserved with no new schema.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from polyecho.discovery._markets import fetch_hot_markets_gamma
from polyecho.discovery._perf import (
    DISCOVERY_CYCLE_TARGET_SECONDS,
    perf_timer,
)
from polyecho.discovery.frequency import (
    FrequencyStats,
    StrategyType,
    classify_strategy,
    compute_frequency_stats,
)
from polyecho.discovery.riskflags import detect_risk_flags
from polyecho.discovery.scoring import (
    final_score,
    profit_score,
    replicability_score,
    score_metadata,
)
from polyecho.shared.datasource import (
    ActivityRecord,
    DataSource,
    FixtureDataSource,
    MarketMetadataCache,
)
from polyecho.shared.db import session_scope
from polyecho.shared.events import (
    EventType,
    PushPayload,
    Severity,
    emit_event,
)
from polyecho.shared.models import CandidateLeader, CandidateScoreHistory, Event

logger = logging.getLogger(__name__)

# --- Fixture-mode DB-persistence gate (防再種) -----------------------------
#
# WHY: in ``POLYECHO_DATASOURCE=fixture`` the worker ranks the *synthetic*
# fixture leaderboard (demo wallets like ``0xscalper2000…``) and would
# UPSERT them into the operator's ``candidate_leaders`` — auto-polluting a
# live operator DB with fake leaders.  Fixture mode is the default safe /
# test path and must NOT auto-seed the DB.
#
# DESIGN: a process-wide boolean, default ``True`` so the gate is a NO-OP
# unless something explicitly closes it.  ONLY the deployed entrypoint
# (:func:`polyecho.discovery.__main__.main`) closes it — UNCONDITIONALLY
# whenever the *resolved* datasource is a ``FixtureDataSource`` (there is
# NO operator opt-in to write fixture data to the DB).  Because the gate
# defaults open and is flipped ONLY by ``main()``, every test that drives
# the worker / upsert helpers directly (their own mocked ``session_scope``,
# never through ``main()``) is unaffected — the entire discovery suite
# legitimately keeps exercising persistence with fixture data.  The gate is
# the deployed-entrypoint/datasource WRITE boundary, nothing weaker.
_persist_enabled: bool = True


def set_persistence_enabled(enabled: bool) -> None:
    """Open (``True``) / close (``False``) the DB-persistence gate.

    Called once by :func:`polyecho.discovery.__main__.main` before any
    pass runs.  Idempotent; safe to call repeatedly.  Not part of the
    per-wallet hot path (a single module global read), so it adds no
    measurable overhead and keeps determinism (no I/O, no clock).
    """
    global _persist_enabled
    _persist_enabled = enabled


def persistence_enabled() -> bool:
    """Whether the discovery worker may write candidate rows to the DB.

    ``True`` by default (every direct test path); flipped to ``False`` by
    the deployed entrypoint whenever the resolved datasource is a
    ``FixtureDataSource`` so fixture mode never auto-seeds the operator DB.
    """
    return _persist_enabled


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


# --- Phase-3 performance / anti-duplication tuning ------------------------
#
# L1 — bounded concurrency.  The per-wallet pipeline issues several
# independent network reads; running every wallet strictly sequentially
# made a full leaderboard sweep O(N) round-trips wall-clock.  We fan the
# wallet loop out with ``asyncio.gather`` but cap the in-flight count at
# ``_DISCOVERY_CONCURRENCY`` via a single module-level semaphore so we
# never open an unbounded number of sockets against the Polymarket
# data-api (politeness + memory bound).  Raised 8 → 12: cold-start
# profiling showed a 200-entry board took 4 daily sweeps to fully detail
# at budget 50; per-wallet cost is ≈ 2 activity pages + 1 positions read,
# so 120 wallets × 3 reads @ concurrency 12 still completes well inside
# the §7.1 30-min full-recompute budget while ~halving wall-clock vs 8.
# The live source still paginates internally per call, so 12 in-flight
# wallets is a bounded, polite socket ceiling (not 12×N).
_DISCOVERY_CONCURRENCY = 12

# L2 — daily full-scoring staleness gate.  A wallet whose ``last_active_at``
# is unchanged AND whose ``last_seen_ts`` cursor is newer than
# ``now - _DISCOVERY_STALENESS_DAYS`` is skipped by the *daily* full-score
# path (its rolling 7/30/90d stats cannot have moved).  This is a GATE,
# not a windowed fetch — the daily path still needs the full window when
# it does run (rolling stats), so we never shrink that fetch.
_DISCOVERY_STALENESS_DAYS = 1

# L3 — tiered detail budget.  Phase 1 of the daily sweep upserts a THIN
# row for every leaderboard entry (no per-wallet network call).  The
# expensive full ``_process_wallet`` then runs only for the wallets that
# are new / crossed a rank·pnl delta / are staler than
# ``_DISCOVERY_STALE_DETAIL_DAYS`` — capped at ``_DISCOVERY_DETAIL_BUDGET``
# per daily sweep so a single sweep can never blow the §7.1 time budget.
# Raised 50 → 120: at 50 a cold-start 200-entry leaderboard needed 4
# daily sweeps before every entry had been fully detailed once.  Per
# wallet ≈ 2 activity pages + 1 positions read; 120 × 3 reads at
# concurrency 12 (see ``_DISCOVERY_CONCURRENCY``) stays comfortably
# inside the §7.1 30-min full-recompute budget, cutting cold-start
# convergence to ≤ 2 sweeps without risking the time ceiling.
_DISCOVERY_DETAIL_BUDGET = 120

# A thin row is promoted to a full re-score when its leaderboard rank
# improved by ≥ this many places OR its leaderboard pnl moved by ≥ this
# fraction since the last full score (a materially changed trader is
# worth the expensive recompute even if not yet stale).
_DISCOVERY_RANK_DELTA = 5
_DISCOVERY_PNL_DELTA_FRAC = 0.20

# A thin row with no other trigger is still force-promoted to a full
# re-score once it is this many days stale (so a quiet but still-tracked
# wallet is eventually refreshed even if it never moves on the board).
_DISCOVERY_STALE_DETAIL_DAYS = 7

# ``last_seen_ts`` means "every trade up to AND INCLUDING this ts has
# already been processed".  The frozen datasource ``start=`` filter is
# INCLUSIVE (``timestamp >= start``), so to make the windowed re-scan
# pick up only the *un-seen* tail we advance the lower bound by one
# microsecond past the cursor — otherwise the boundary trade (ts ==
# cursor) is re-fetched and the wallet is re-surfaced every hour
# (the exact duplication this layer removes).  1 µs is below Polymarket
# ts resolution so it can never skip a genuinely newer trade.
_CURSOR_EXCLUSIVE_EPSILON = timedelta(microseconds=1)


# L1 concurrency gate.  An ``asyncio.Semaphore`` binds to the event loop
# that is running when it is FIRST awaited; creating one at import time
# would bind it to whatever loop existed then and raise
# "bound to a different event loop" the moment discovery runs under a
# different loop (e.g. a fresh per-test loop, or a service that builds
# its own loop after import).  So the semaphore is created lazily and
# cached PER RUNNING LOOP — global within a loop (the cap is
# process-global for a given event loop) yet loop-safe.
_loop_sems: dict[Any, asyncio.Semaphore] = {}


def _get_semaphore() -> asyncio.Semaphore:
    """Return the ``_DISCOVERY_CONCURRENCY`` semaphore for the *current*
    running loop, creating (and caching) it on first use in that loop."""
    loop = asyncio.get_running_loop()
    # Prune semaphores whose loop has been closed (e.g. finished test
    # loops) so this cache never grows unbounded across many loops.
    for stale in [lp for lp in _loop_sems if lp.is_closed()]:
        del _loop_sems[stale]
    sem = _loop_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_DISCOVERY_CONCURRENCY)
        _loop_sems[loop] = sem
    return sem


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _bounded_gather(
    keys: list[str],
    coro_factory: Callable[[str], Awaitable[None]],
) -> None:
    """Run ``coro_factory(key)`` for every key, ≤ N concurrently.

    L1 determinism contract: this is used ONLY for the independent
    per-wallet *side-effecting* pipeline (each call upserts its own
    wallet's rows under its own ``SELECT … FOR UPDATE`` txn — there is no
    shared in-memory accumulator), so concurrent completion order does
    not affect persisted state.  Callers that build an in-memory result
    set MUST still feed the DB in a stable (sorted) order — every such
    call site sorts ``keys`` before handing them here.
    """

    sem = _get_semaphore()

    async def _guarded(key: str) -> None:
        async with sem:
            await coro_factory(key)

    await asyncio.gather(*(_guarded(k) for k in keys))


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

    unavailable_inputs
        PolyHermes deep-dive B2 — audit list of scoring-input names whose
        SOURCE DATA was genuinely missing (a failed/empty fetch), as
        opposed to a real measured zero.  ``win_rate`` lands here when no
        priced SELL exists to measure a win/lose ratio; ``sharpe`` lands
        here when there are < 2 daily-PnL points to compute a deviation.
        When the wallet has NO trades at all every input is unavailable.
        :func:`~polyecho.discovery.scoring.score_metadata` reads this list
        to set the ``low_confidence`` flag — a consumer must not treat a
        score built on unavailable inputs as a confirmed signal.
    """
    trades = [r for r in activity if r.activity_type == "TRADE"]

    if not trades:
        # No trades at all: NOTHING was measured.  The numeric defaults
        # below keep ``profit_score`` from crashing, but ``unavailable_inputs``
        # records that they are placeholders, NOT measured zeros — so the
        # score is flagged low-confidence and never read as "verified".
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
            "unavailable_inputs": [
                "pnl_usd",
                "win_rate",
                "sharpe",
                "max_drawdown",
                "consistency",
                "specialization",
            ],
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

    # PolyHermes deep-dive B2 — audit list of inputs that could NOT be
    # measured (no data), kept distinct from a real measured zero so the
    # score can be flagged low-confidence rather than silently trusted.
    unavailable_inputs: list[str] = []

    # ── win_rate ──────────────────────────────────────────────────────────────
    # P1-9b: a SELL whose ``usdc_size is None`` is *missing data*, NOT a
    # zero-value (losing) close.  The old code counted it via
    # ``(r.usdc_size or 0.0) > 0`` → False → silently a LOSS, which
    # systematically deflated win_rate (→ profit_score, 20 % weight).
    # Only SELLs with a known notional are admissible to the win/lose
    # denominator; ``usdc_size is None`` rows are excluded entirely.
    priced_sell_usdc: list[float] = [
        r.usdc_size for r in trades if r.side == "SELL" and r.usdc_size is not None
    ]
    wins = sum(1 for u in priced_sell_usdc if u > 0)
    win_rate = wins / len(priced_sell_usdc) if priced_sell_usdc else 0.0
    # B2: with no priced SELL there is NO win/lose ratio to measure — the
    # 0.0 above is a placeholder, not a 0 % win-rate.  Flag it unavailable
    # so a pure-BUY or unpriced sample is not read as a confirmed loser.
    if not priced_sell_usdc:
        unavailable_inputs.append("win_rate")

    # ── Sharpe (daily, assume rf=0) ───────────────────────────────────────────
    if len(pnl_series) >= 2:
        mean_d = sum(pnl_series) / len(pnl_series)
        var_d = sum((x - mean_d) ** 2 for x in pnl_series) / (len(pnl_series) - 1)
        std_d = math.sqrt(var_d) if var_d > 0 else 1e-9
        sharpe = (mean_d / std_d) * math.sqrt(365.0)
    else:
        # B2: < 2 daily-PnL points means a deviation cannot be computed —
        # the 0.0 is "not enough data", NOT a measured Sharpe of zero.
        sharpe = 0.0
        unavailable_inputs.append("sharpe")

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
        # B2: empty when every input was measured; lists input names whose
        # source data was missing so the score can be flagged low-confidence.
        "unavailable_inputs": unavailable_inputs,
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
    last_seen_ts: datetime | None = None,
) -> tuple[bool, datetime | None]:
    """Upsert a CandidateLeader row and append a CandidateScoreHistory row.

    Args:
        last_seen_ts: Phase-3 (L2) discovery cursor high-water mark to
            persist for this wallet.  Folded into the SAME locked txn as
            the upsert; the column is advanced *monotonically*
            (``max(new, prior)``) so a windowed re-scan can never move
            the cursor backwards.  ``None`` leaves the stored cursor
            unchanged (callers that do not own the cursor — e.g. the
            leaderboard/hot-market daily paths — pass ``None``).

    Returns:
        ``(emitted_high_score, prior_last_seen_ts)``.

        ``emitted_high_score`` is ``True`` iff a ``CANDIDATE_HIGH_SCORE``
        event was emitted for this wallet on this call (an upward
        threshold crossing).  Used only to feed the per-pass ``emitted``
        heartbeat counter (task 2); it does not affect persistence or
        scoring.

        ``prior_last_seen_ts`` is the value of ``CandidateLeader.
        last_seen_ts`` that was read (under the FOR UPDATE lock) BEFORE
        this call advanced it — folded out of the existing locked SELECT
        so the hourly windowed scan never needs a second query (L2).

    Uses raw INSERT … ON CONFLICT for PostgreSQL; falls back to SELECT + add
    for SQLite (tests with in-memory DB).

    Concurrency (P1-9c — R10 C1/C2 hardened)
    ----------------------------------------
    The prior-score read, the upward-crossing decision and BOTH row
    writes happen inside ONE transaction while holding a
    ``SELECT … FOR UPDATE`` row lock on the candidate.  Two concurrent
    same-wallet runs serialise on that lock: the second blocks until the
    first commits, then re-reads ``prev_score`` (now ≥ threshold) and the
    upward crossing is False.

    R10 C1 — the ``candidate_score_history`` row is written with a real
    ``INSERT … ON CONFLICT (wallet_address, date) DO UPDATE`` (dialect-
    aware: ``postgresql``/``sqlite``), so even if two same-wallet+same-UTC
    -day runs were NOT serialised by the candidate lock (e.g. a brand-new
    wallet where ``FOR UPDATE`` has no row to lock yet), the history write
    is atomic at the DB and can never raise a duplicate-key error or
    create a duplicate row.

    R10 C2 — ``_safe_emit_high_score`` is NO LONGER awaited while the
    txn + ``FOR UPDATE`` lock are held.  The *decision* (crossed_high) is
    taken inside the locked txn; the emit is performed AFTER
    ``session_scope`` exits/commits and the row lock is released, so a
    NOTIFY/push hang can never hold the lock and stall every concurrent
    same-wallet ``_process_wallet``.  (On SQLite ``FOR UPDATE`` is a
    no-op, which is fine — tests don't exercise true row contention.)
    """
    # 防再種 WRITE BOUNDARY: in fixture-datasource mode (no opt-in) the
    # deployed entrypoint closes the persistence gate so this synthetic
    # fixture candidate is NEVER written to the operator DB.  Returning the
    # brand-new-wallet sentinel ``(False, None)`` keeps every caller's
    # control flow byte-identical to "wallet not in DB yet": no high-score
    # event, and ``prior_last_seen_ts=None`` so the hourly windowed scan
    # just falls back to its 24 h trailing window (exactly the empty-DB
    # path).  The gate defaults OPEN, so direct-call tests are unaffected.
    if not _persist_enabled:
        logger.debug(
            "fixture_persist_skipped",
            extra={"wallet": wallet, "source": source, "fn": "_upsert_candidate"},
        )
        return False, None

    # P1-9a: key score-history by the UTC calendar day, NOT the server's
    # LOCAL date.  ``date.today()`` uses the process-local timezone, so a
    # non-UTC container would write history under the wrong day while every
    # other timestamp here uses ``datetime.now(tz=UTC)`` — a silent,
    # production-corrupting drift.  Use the UTC date explicitly.
    today = datetime.now(tz=UTC).date()
    # Money-safety: an ``insider_suspect`` candidate is parked at the C1
    # ladder terminal state ``retired`` so the state machine
    # (``polyecho.paper.state_machine.advance``) short-circuits with
    # ``reason="terminal_state"`` and never auto-bootstraps it into paper
    # trading. This replaces the prior ``user_status='rejected'`` gate
    # (the column was removed); the exclusion semantic is preserved with
    # no new schema.
    is_insider = "insider_suspect" in risk_flags
    insider_lifecycle_state = "retired" if is_insider else None

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

        # L2: read the EXISTING discovery cursor out of the SAME locked
        # SELECT (no extra query) and advance it monotonically.  A
        # windowed re-scan must never regress the high-water mark, so the
        # persisted value is max(new seen ts, prior cursor).  When the
        # caller passes ``last_seen_ts=None`` (it does not own the cursor)
        # the stored value is left exactly as-is.
        prior_last_seen_ts: datetime | None = (
            existing.last_seen_ts if existing is not None else None
        )
        new_last_seen_ts: datetime | None = prior_last_seen_ts
        if last_seen_ts is not None:
            new_last_seen_ts = (
                last_seen_ts
                if prior_last_seen_ts is None
                else max(prior_last_seen_ts, last_seen_ts)
            )

        if existing is None:
            leader_kwargs: dict[str, Any] = dict(
                wallet_address=wallet,
                display_name=display_name,
                sources=[source],
                first_discovered_at=datetime.now(tz=UTC),
                last_active_at=last_active_at,
                last_seen_ts=new_last_seen_ts,
                stats_7d=stats_7d,
                stats_30d=stats_30d,
                stats_90d=stats_90d,
                frequency_stats=freq_stats_30d.to_dict(),
                strategy_type=strategy_type.value,
                profit_score=p_score,
                replicability_score=r_score,
                total_score=f_score,
                risk_flags=risk_flags,
            )
            # Money-safety: insider_suspect is parked at the ladder
            # terminal ``retired`` so the C1 state machine can never
            # auto-bootstrap it. Non-insider candidates keep the column
            # default (``discovered``) and flow through the ladder.
            if insider_lifecycle_state is not None:
                leader_kwargs["lifecycle_state"] = insider_lifecycle_state
            leader = CandidateLeader(**leader_kwargs)
            session.add(leader)
        else:
            # Merge sources (deduplicated)
            merged_sources = sorted(set(existing.sources or []) | {source})
            existing.sources = merged_sources
            # first_discovered_at: keep original
            existing.last_active_at = last_active_at or existing.last_active_at
            # L2: monotonic cursor advance (never regress on a windowed
            # re-scan).  ``new_last_seen_ts`` already folds in the prior
            # value, so an unconditional assign is safe and is a no-op
            # when the caller passed ``last_seen_ts=None``.
            existing.last_seen_ts = new_last_seen_ts
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
            # Money-safety: only ever park at terminal ``retired`` on an
            # insider_suspect detection; never auto-resurrect a retired
            # candidate. ``retired`` is the C1 ladder terminal state
            # (state machine short-circuits with ``terminal_state``).
            if insider_lifecycle_state == "retired":
                existing.lifecycle_state = "retired"

        # ── CandidateScoreHistory upsert — R10 C1 (atomic ON CONFLICT) ────────
        # The OLD code did an UNLOCKED ``SELECT … then session.add(...)``:
        # two concurrent same-wallet+same-UTC-day runs could both observe
        # ``hist is None`` (the candidate FOR UPDATE lock does NOT cover a
        # *different* table's row, and for a brand-new wallet there is no
        # candidate row to lock at all) → duplicate-key error or a
        # duplicate history row.  Fix: a real, dialect-aware
        # ``INSERT … ON CONFLICT (wallet_address, date) DO UPDATE`` makes
        # the write atomic at the database — exactly one row per
        # (wallet, UTC day) regardless of concurrency.
        hist_values = {
            "wallet_address": wallet,
            "date": today,
            "total_score": f_score,
            "stats": stats_30d,
        }
        dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
        if dialect_name == "sqlite":
            sqlite_hist = sqlite_insert(CandidateScoreHistory).values(**hist_values)
            await session.execute(
                sqlite_hist.on_conflict_do_update(
                    index_elements=["wallet_address", "date"],
                    set_={
                        "total_score": sqlite_hist.excluded.total_score,
                        "stats": sqlite_hist.excluded.stats,
                    },
                )
            )
        else:
            pg_hist = pg_insert(CandidateScoreHistory).values(**hist_values)
            await session.execute(
                pg_hist.on_conflict_do_update(
                    index_elements=["wallet_address", "date"],
                    set_={
                        "total_score": pg_hist.excluded.total_score,
                        "stats": pg_hist.excluded.stats,
                    },
                )
            )

        # R10 C2: decide the emit INSIDE the locked txn (atomic
        # read→decide), but DO NOT await it here — emitting while the
        # FOR UPDATE lock is held means a NOTIFY/push hang would pin the
        # lock and stall every concurrent same-wallet _process_wallet.
        # The actual emit happens AFTER session_scope() exits/commits and
        # the row lock is released (see below).  Serialisation is still
        # guaranteed: a concurrent same-wallet run blocks on the candidate
        # FOR UPDATE row lock until THIS txn commits, then re-reads
        # prev_score (now ≥ threshold) so crossed_high is False — the
        # event still fires EXACTLY ONCE per upward crossing.
        # Money-safety: do NOT fire a high-score event for an insider_suspect
        # candidate. The leader is parked at the terminal ``retired`` state
        # for the same reason — a notification would be a misleading signal
        # for a wallet that will never be auto-promoted by the ladder.
        should_emit_high_score = crossed_high and not is_insider

    # ── CANDIDATE_HIGH_SCORE emit — AFTER the txn commits & lock released ──
    # R10 C2: the FOR UPDATE row lock is dropped the moment session_scope()
    # exited above.  ``_safe_emit_high_score`` is best-effort (a NOTIFY
    # hiccup is logged & swallowed); it can no longer hold the lock or
    # block a concurrent same-wallet run no matter how long the push
    # backend takes.
    if should_emit_high_score:
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
            "insider_suspect": is_insider,
        },
    )

    # Tell the caller whether a high-score event fired (for the per-pass
    # ``emitted`` heartbeat counter only — no behavioural effect) and the
    # PRIOR cursor value read under the lock (L2 — lets the hourly scan
    # advance its window without a second query).
    return should_emit_high_score, prior_last_seen_ts


async def _insert_drift_event(wallet: str, strategy_30d: str, strategy_7d: str) -> None:
    """Insert a strategy_drift Event row (PRD §5.8)."""
    # 防再種: a drift Event is a downstream side-effect of a *persisted*
    # fixture candidate.  When the persistence gate is closed (fixture
    # mode, no opt-in) the candidate itself was never written, so its
    # synthetic drift Event must not pollute the operator DB either.
    if not _persist_enabled:
        logger.debug(
            "fixture_persist_skipped",
            extra={"wallet": wallet, "fn": "_insert_drift_event"},
        )
        return

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


async def _upsert_thin(
    wallet: str,
    source: str,
    *,
    display_name: str | None,
    rank: int | None,
    leaderboard_pnl: float | None,
    leaderboard_volume: float | None,
) -> tuple[datetime | None, datetime | None, float | None, dict[str, Any]]:
    """L3 Phase-1 THIN upsert — identity + leaderboard signal only.

    The daily sweep can afford to touch EVERY leaderboard entry because
    this path makes NO per-wallet network call: it persists only what
    ``get_leaderboard`` already returned (rank + pnl + volume) plus the
    discovery ``source``.  It deliberately does NOT write the rolling
    ``stats_*`` / ``frequency_stats`` / scores — those need the expensive
    ``_process_wallet`` fetch, which Phase 2 runs for the budgeted subset
    only.

    The leaderboard pnl/vol snapshot is stored under
    ``stats_leaderboard`` inside ``backtest_default`` (a free-form JSONB
    column already in the schema — NO schema change for the thin signal);
    a brand-new wallet gets a row so Phase-2 selection / the dashboard
    can see it immediately even before its first full score.

    Returns the PRIOR ``(last_active_at, last_seen_ts, total_score,
    prior_thin_signal)`` read under a ``SELECT … FOR UPDATE`` lock (one
    query) so the caller can decide — without any extra round-trip —
    whether this wallet is new / materially moved / stale enough to
    spend a Phase-2 detail slot.  ``prior_thin_signal`` is the
    previously-stored ``{rank, leaderboard_pnl, leaderboard_volume}``
    (``{}`` for a brand-new wallet) — the delta baseline.
    """
    # 防再種 WRITE BOUNDARY (Phase-1 thin path): in fixture mode without
    # opt-in the deployed entrypoint closes the gate so this synthetic
    # leaderboard entry is NEVER written.  Return the brand-new-wallet
    # sentinel ``(None, None, None, {})`` — identical to "wallet not in DB
    # yet", so ``discover_leaderboard``'s staleness/needs-detail predicates
    # behave exactly as on an empty DB and the (also-gated)
    # ``_upsert_candidate`` downstream simply writes nothing either.
    if not _persist_enabled:
        logger.debug(
            "fixture_persist_skipped",
            extra={"wallet": wallet, "source": source, "fn": "_upsert_thin"},
        )
        return None, None, None, {}

    async with session_scope() as session:
        result = await session.execute(
            select(CandidateLeader)
            .where(CandidateLeader.wallet_address == wallet)
            .with_for_update()
        )
        existing: CandidateLeader | None = result.scalar_one_or_none()

        prior_last_active_at = existing.last_active_at if existing else None
        prior_last_seen_ts = existing.last_seen_ts if existing else None
        prior_total_score = (
            float(existing.total_score)
            if existing is not None and existing.total_score is not None
            else None
        )
        prior_thin_signal: dict[str, Any] = {}
        if existing is not None and existing.backtest_default:
            raw_prior = existing.backtest_default.get("stats_leaderboard")
            if isinstance(raw_prior, dict):
                prior_thin_signal = dict(raw_prior)

        thin_signal = {
            "rank": rank,
            "leaderboard_pnl": leaderboard_pnl,
            "leaderboard_volume": leaderboard_volume,
        }
        if existing is None:
            leader = CandidateLeader(
                wallet_address=wallet,
                display_name=display_name,
                sources=[source],
                first_discovered_at=datetime.now(tz=UTC),
                backtest_default={"stats_leaderboard": thin_signal},
            )
            session.add(leader)
        else:
            existing.sources = sorted(set(existing.sources or []) | {source})
            existing.display_name = display_name or existing.display_name
            merged = dict(existing.backtest_default or {})
            merged["stats_leaderboard"] = thin_signal
            existing.backtest_default = merged

    return (
        prior_last_active_at,
        prior_last_seen_ts,
        prior_total_score,
        prior_thin_signal,
    )


async def _read_cursors(wallets: set[str]) -> dict[str, datetime | None]:
    """Batch-read ``last_seen_ts`` for *wallets* in ONE query (L2).

    The hourly large-new-position scan needs each wallet's prior cursor
    to push ``start=last_seen_ts`` down into the detection
    ``get_user_activity`` call.  Reading them one-by-one would re-add the
    per-wallet round-trip the redesign removes, so this issues a single
    ``SELECT wallet_address, last_seen_ts WHERE wallet_address IN (...)``.
    Wallets with no row (or a NULL cursor) map to ``None`` → the caller
    falls back to the 24 h trailing window for them (first-seen
    behaviour, unchanged).
    """
    if not wallets:
        return {}
    cursors: dict[str, datetime | None] = {}
    # Best-effort: a cursor-read hiccup must NEVER break the hourly scan.
    # On any failure we return ``{}`` → every wallet falls back to the
    # 24 h trailing window (exactly the pre-Phase-3 behaviour), so the
    # optimisation degrades safely instead of failing the pass.  This
    # mirrors the worker-wide best-effort discipline
    # (``_safe_emit_high_score`` / ``_log_cycle_duration``).
    try:
        async with session_scope() as session:
            result = await session.execute(
                select(
                    CandidateLeader.wallet_address,
                    CandidateLeader.last_seen_ts,
                ).where(CandidateLeader.wallet_address.in_(sorted(wallets)))
            )
            for wallet_address, last_seen_ts in result.all():
                cursors[wallet_address] = last_seen_ts
    except Exception as exc:  # noqa: BLE001 - cursor read is best-effort
        logger.warning(
            "discovery_cursor_read_failed_falling_back_to_window",
            extra={"error": str(exc), "pool_size": len(wallets)},
        )
        return {}
    return cursors


def _daily_staleness_skip(
    *,
    prior_last_seen_ts: datetime | None,
    prior_total_score: float | None,
    prior_leaderboard_pnl: float | None,
    new_leaderboard_pnl: float | None,
    now: datetime,
) -> bool:
    """L2 daily full-scoring STALENESS GATE (pure — deterministic).

    The daily path's rolling 7/30/90d stats can only change if the
    wallet realised new P&L since the last full score.  We must decide
    this WITHOUT a per-wallet fetch (that fetch is the cost the redesign
    removes), so the cheap, already-available proxy for "``last_active_at``
    unchanged" is the leaderboard ``pnl`` from ``get_leaderboard`` (Phase
    1 stored it as the thin signal — a wallet whose leaderboard pnl is
    bit-identical this sweep has realised no new closed P&L).

    Skip the expensive full re-score when BOTH hold:

    * leaderboard ``pnl`` is **unchanged** vs the prior stored thin
      signal (proxy: no new realised activity since the last full
      score), AND
    * ``last_seen_ts`` is **within the daily cadence** (newer than
      ``now - _DISCOVERY_STALENESS_DAYS`` — scanned recently enough).

    This is a GATE, not a windowed fetch: when it does NOT skip, the
    caller still does the FULL-window score (rolling stats need the whole
    window — never shrunk).  A wallet never fully scored
    (no ``prior_total_score``) is NEVER gated here — it must get a first
    real score (that decision belongs to ``_needs_full_detail``'s NEW
    branch).
    """
    if prior_total_score is None:
        return False  # never scored → never gate (let NEW logic decide)
    if prior_leaderboard_pnl is None or new_leaderboard_pnl is None:
        return False  # can't prove "unchanged" → don't gate (safe default)
    if new_leaderboard_pnl != prior_leaderboard_pnl:
        return False  # realised new P&L → stats moved → must re-score
    if prior_last_seen_ts is None:
        return False  # no cursor → not provably within cadence → re-score
    # leaderboard pnl unchanged AND cursor within the daily cadence → the
    # rolling stats cannot have moved; safe to skip this sweep.
    return prior_last_seen_ts >= now - timedelta(days=_DISCOVERY_STALENESS_DAYS)


def _needs_full_detail(
    *,
    prior_last_active_at: datetime | None,
    prior_last_seen_ts: datetime | None,
    prior_total_score: float | None,
    prior_thin_signal: dict[str, Any],
    rank: int | None,
    leaderboard_pnl: float | None,
    now: datetime,
) -> bool:
    """L3 Phase-2 selection predicate (pure — deterministic, no I/O).

    Promote a thin row to the expensive full ``_process_wallet`` when it
    is *new*, has *materially moved* on the board, or is *stale*:

    * NEW                — never fully scored yet (no prior total_score
      AND no prior cursor): always worth a first real score.
    * MATERIALLY MOVED   — leaderboard rank improved (numerically
      decreased) by ≥ ``_DISCOVERY_RANK_DELTA`` OR leaderboard pnl moved
      by ≥ ``_DISCOVERY_PNL_DELTA_FRAC`` (relative) vs the prior stored
      thin signal.
    * STALE              — ``last_seen_ts`` (or, lacking it,
      ``last_active_at``) is older than ``_DISCOVERY_STALE_DETAIL_DAYS``.

    Everything else is left as the cheap thin row this sweep.  Every
    branch is pure on its inputs → two identical sweeps select the
    identical subset (determinism).
    """
    if prior_total_score is None and prior_last_seen_ts is None:
        return True  # never fully scored — first real score is mandatory

    stale_ref = prior_last_seen_ts or prior_last_active_at
    if stale_ref is None:
        return True
    if stale_ref < now - timedelta(days=_DISCOVERY_STALE_DETAIL_DAYS):
        return True

    # Materially-moved checks vs the prior stored thin signal.  Missing
    # data ⇒ no trigger (never spuriously spend the budget).
    prev_rank = prior_thin_signal.get("rank")
    if (
        rank is not None
        and isinstance(prev_rank, int)
        and prev_rank - rank >= _DISCOVERY_RANK_DELTA
    ):
        return True  # climbed ≥ N places up the board

    prev_pnl = prior_thin_signal.get("leaderboard_pnl")
    if (
        leaderboard_pnl is not None
        and isinstance(prev_pnl, (int, float))
        and abs(prev_pnl) > 0.0
        and abs(leaderboard_pnl - prev_pnl) / abs(prev_pnl) >= _DISCOVERY_PNL_DELTA_FRAC
    ):
        return True  # pnl swung materially either way

    return False


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
        # C4 (PolyHermes deep-dive): persisted market-metadata cache
        # (cache -> markets DB table -> remote).  Composed with the FROZEN
        # datasource — discover_hot_market_holders reads market metadata
        # through it so a market's question/neg_risk/tick_size/end_date is
        # persisted once and re-served (resolved markets cached forever).
        self._market_cache = MarketMetadataCache(datasource)
        # Cache of known wallet addresses across all sources (used by hourly scan)
        self._known_wallets: set[str] = set()
        # Per-pass discovery counters for the F0 ``discovery_heartbeat``
        # (task 2).  ``scanned``  = wallets the pipeline attempted,
        # ``scored`` = wallets that completed scoring + upsert,
        # ``emitted`` = wallets that crossed the high-score threshold this
        # pass.  Reset (drained) once per heartbeat by
        # :meth:`drain_pass_counters` so each heartbeat reports only the
        # work done since the previous one.
        #
        # Per-SOURCE counters (``hot_market_holders`` / ``leaderboard`` /
        # ``large_new_positions``) are incremented once per wallet
        # ``_process_wallet`` handles, keyed by its ``source``.  They
        # surface in the F0 heartbeat ``counters`` sub-dict (free-form;
        # the web /status reader only consumes known keys, so adding keys
        # is additive-safe) so a *dead source* (e.g. live hot-markets
        # 404) shows ``hot_market_holders: 0`` instead of the whole
        # worker merely looking healthy off the other sources.
        self._pass_counters: dict[str, int] = {
            "scanned": 0,
            "scored": 0,
            "emitted": 0,
            "hot_market_holders": 0,
            "leaderboard": 0,
            "large_new_positions": 0,
        }
        # The exact reset baseline drained per heartbeat — kept in one
        # place so the source keys can never drift out of sync with the
        # ctor (a missing key would make a dead source *vanish* from the
        # heartbeat instead of reporting 0).
        self._pass_counter_zero: dict[str, int] = dict.fromkeys(self._pass_counters, 0)

    @property
    def datasource_name(self) -> str:
        """Concrete datasource class name (e.g. ``FixtureDataSource``).

        Used to populate the F0 heartbeat ``datasource`` field.
        """
        return type(self._ds).__name__

    def drain_pass_counters(self) -> dict[str, int]:
        """Return a snapshot of the per-pass counters and reset them to 0.

        Called once per heartbeat (task 2) so each ``discovery_heartbeat``
        reflects only the candidates scanned/scored/emitted since the
        previous heartbeat.  Returning a *copy* and zeroing in place keeps
        this safe to call between passes.
        """
        snapshot = dict(self._pass_counters)
        # Reset to the full zero baseline (incl. the per-source keys) so
        # every heartbeat reports the same key set — a source that did
        # NOTHING this pass still reports ``<source>: 0`` rather than
        # disappearing from the heartbeat.
        self._pass_counters = dict(self._pass_counter_zero)
        return snapshot

    # ------------------------------------------------------------------
    # Core per-wallet pipeline
    # ------------------------------------------------------------------

    async def _process_wallet(
        self,
        wallet: str,
        source: str,
        *,
        display_name: str | None = None,
        start: datetime | None = None,
    ) -> datetime | None:
        """Full pipeline for a single wallet: fetch → compute → persist.

        Args:
            start: L2 windowed-cursor lower bound.  ONLY the hourly
                large-new-position scan passes this (``start=last_seen_ts``)
                — that path is pure ">$threshold trade since cursor?"
                detection, so a windowed TRADE fetch is correct and
                drastically cuts the re-scan duplication.  The daily
                full-scoring path passes ``start=None`` (rolling 7/30/90d
                stats need the full window — it must NOT be shrunk; its
                cheapness comes from the L2 staleness GATE in the caller,
                not from a smaller fetch).

        Returns:
            The PRIOR ``CandidateLeader.last_seen_ts`` for this wallet
            (read under the upsert's FOR UPDATE lock, no extra query).
            ``None`` when the wallet is brand-new or the fetch failed.

        Steps
        -----
        1. Fetch activity (TRADE + REDEEM) + open positions via
           datasource — the 3 independent reads run as ONE
           ``asyncio.gather`` (L1).
        2. Slice into 7d / 30d / 90d windows.
        3. Compute stats + frequency stats for each window.
        4. Classify strategy (30d) and detect drift (30d vs 7d).
        5. Detect risk flags.
        6. Score (profit + replicability → final).
        7. Upsert DB rows (advancing the L2 ``last_seen_ts`` cursor).
        8. Insert ``strategy_drift`` Event if flagged.
        """
        logger.debug("processing wallet", extra={"wallet": wallet, "source": source})

        # Per-pass heartbeat counter (task 2): every wallet the pipeline
        # attempts is "scanned" — incremented before the fetch so a
        # fetch failure (early return below) is still counted as scanned
        # but not scored.
        self._pass_counters["scanned"] += 1
        # Per-SOURCE attempt counter (same gate as ``scanned``): a wallet
        # the pipeline *attempts* from ``source`` is counted here even if
        # its fetch fails, so a dead source reports a non-zero scan only
        # when it actually fed wallets in.  ``.get`` default keeps an
        # unrecognised future source from KeyError-ing the pipeline.
        if source in self._pass_counters:
            self._pass_counters[source] += 1

        try:
            # L1: the 3 independent reads have no data dependency on each
            # other — issue them concurrently (one gather) instead of
            # three serial awaits.  ``return_exceptions=False`` so any
            # failure still funnels into the existing except-block
            # (unchanged failure semantics: scanned-not-scored, early
            # return).
            activity_90d, redeems_90d, positions = await asyncio.gather(
                self._ds.get_user_activity(wallet, start=start, activity_type="TRADE", limit=None),
                self._ds.get_user_activity(wallet, start=start, activity_type="REDEEM", limit=None),
                self._ds.get_positions(wallet, size_threshold=0.0),
            )
            # Determinism (L1): the live/fixture sources return
            # TIMESTAMP-DESC; downstream windowing/scoring is
            # order-independent, but re-sort into a single stable
            # (timestamp, then activity_type) order BEFORE any
            # stats/persistence so two fixture runs are byte-identical
            # regardless of gather completion order.
            all_activity: list[ActivityRecord] = sorted(
                list(activity_90d) + list(redeems_90d),
                key=lambda r: (r.timestamp, r.activity_type),
            )
        except Exception:
            logger.exception("failed to fetch data for wallet", extra={"wallet": wallet})
            return None

        self._known_wallets.add(wallet)

        now = datetime.now(tz=UTC)
        act_7d = _window_activity(all_activity, days=7, reference=now)
        act_30d = _window_activity(all_activity, days=30, reference=now)
        # R10 I4: the 90d slice was passing RAW unwindowed activity while
        # 7d/30d are _window_activity-sliced.  ``get_user_activity`` is
        # bounded but a live source can still return >90d of history; an
        # unsliced denominator understated ``trades_per_day`` for the 90d
        # window.  Slice it the same way as 7d/30d for a consistent rate.
        act_90d = _window_activity(all_activity, days=90, reference=now)

        stats_7d = _compute_stats(act_7d, window_days=7)
        stats_30d = _compute_stats(act_30d, window_days=30)
        stats_90d = _compute_stats(act_90d, window_days=90)

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

        # PolyHermes deep-dive B2 — stamp the score-trust metadata
        # (score_version + low_confidence flag + reasons) onto stats_30d
        # BEFORE the upsert.  stats_30d is the JSONB that lands in BOTH
        # candidate_leaders.stats_30d AND candidate_score_history.stats,
        # so the version + flag travel inside the existing schema-free
        # payload — no new DB column.  Built from the 30d window because
        # that is the window profit_score is scored on.
        #
        # C1-P4 — also pass the raw final score so score_metadata derives
        # the sample-size-capped ``promotion_score`` (== f_score normally,
        # hard-capped at 59 on a <10-trade sample).  The C1 lifecycle
        # ladder (C1-P3) reads ``stats_30d['promotion_score']`` for its
        # ``score >= 60`` gate; the raw ``total_score`` column stays
        # untouched so the cap is non-destructive and auditable.
        stats_30d.update(score_metadata(stats_30d, total_score=f_score))

        # last_active_at: most recent trade timestamp in 90d window.
        # This same value is the L2 ``last_seen_ts`` cursor high-water
        # mark — the latest TRADE ts we have now processed for this
        # wallet.  Persisting it (monotonically, inside _upsert_candidate)
        # lets the hourly windowed scan pass ``start=last_seen_ts`` next
        # cycle and skip everything already seen (anti-duplication).
        trades_only = [r for r in all_activity if r.activity_type == "TRADE"]
        last_active_at: datetime | None = None
        if trades_only:
            last_active_at = max(r.timestamp for r in trades_only)
        latest_seen_ts = last_active_at

        emitted_high_score, prior_last_seen_ts = await _upsert_candidate(
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
            last_seen_ts=latest_seen_ts,
        )

        # The wallet completed the full compute + upsert → "scored".
        # ``emitted`` tracks high-score threshold crossings this pass.
        self._pass_counters["scored"] += 1
        if emitted_high_score:
            self._pass_counters["emitted"] += 1

        if "strategy_drift" in risk_flags:
            await _insert_drift_event(wallet, strategy_type.value, strategy_7d.value)

        # L2: the PRIOR cursor (read under the upsert lock — no extra
        # query) lets the hourly caller advance its window deterministically.
        return prior_last_seen_ts

    # ------------------------------------------------------------------
    # Source 1: Polymarket Leaderboard (daily)
    # ------------------------------------------------------------------

    async def discover_leaderboard(self) -> None:
        """Fetch top-200 traders from the Polymarket leaderboard (PRD §5.2).

        L3 tiered budget
        -----------------
        * Phase 1 (cheap, every sweep): ``get_leaderboard`` returns
          rank/pnl/volume with NO per-wallet call — upsert a THIN
          ``CandidateLeader`` (identity + rank + leaderboard pnl/vol +
          ``leaderboard`` source) for EVERY entry.
        * Phase 2 (expensive, budgeted): run the full ``_process_wallet``
          ONLY for the wallets that are new / materially moved / stale
          (``_needs_full_detail``), capped at ``_DISCOVERY_DETAIL_BUDGET``
          per sweep.  ``last_seen_ts`` / ``last_active_at`` (read out of
          the thin upsert's locked SELECT — no extra query) drive
          selection.

        Determinism: entries are processed in a stable sorted order and
        the Phase-2 subset is selected by a pure predicate, so two
        fixture sweeps persist byte-identically.
        """
        logger.info("discover_leaderboard: start")
        entries = await self._ds.get_leaderboard(
            category="OVERALL",
            time_period="MONTH",
            order_by="PNL",
            limit=200,
        )
        logger.info("discover_leaderboard: fetched %d entries", len(entries))

        now = datetime.now(tz=UTC)
        # Stable order driving BOTH the Phase-1 thin upsert and the
        # Phase-2 detail-budget slice.  Key = ``(not verified, wallet)``:
        # verified accounts first, then ascending wallet hex as the
        # tie-breaker.  This is still a TOTAL, stable, network-free key
        # (``verified`` is already parsed on ``LeaderboardEntry`` and was
        # otherwise unused) so two fixture sweeps remain byte-identical /
        # order-independent — but when the §7.1 detail budget saturates,
        # the surviving subset is the *verified* leaders rather than
        # whoever happened to have the lowest wallet hex.  ``getattr``
        # default: the frozen DTO always carries ``verified``; treating a
        # missing value as "unverified" keeps the key conservative and
        # robust to minimal entry stubs (a missing flag must never crash
        # the daily sweep — same fail-safe spirit as the rest of L3).
        ordered = sorted(
            entries,
            key=lambda e: (not getattr(e, "verified", False), e.wallet),
        )

        # ── Phase 1 — thin upsert for every entry (no network/wallet call).
        detail_candidates: list[Any] = []
        for entry in ordered:
            (
                prior_la,
                prior_ls,
                prior_score,
                prior_signal,
            ) = await _upsert_thin(
                entry.wallet,
                "leaderboard",
                display_name=entry.username,
                rank=entry.rank,
                leaderboard_pnl=entry.pnl,
                leaderboard_volume=entry.volume,
            )
            # L2 daily staleness GATE first: if the leaderboard pnl is
            # unchanged (proxy for "no new activity") AND the cursor is
            # within the daily cadence, the rolling stats cannot have
            # moved — skip the expensive full score this sweep regardless
            # of the richer L3 predicate.  (NEW wallets are never gated:
            # _daily_staleness_skip returns False when prior_total_score
            # is None, so they still flow into _needs_full_detail.)
            if _daily_staleness_skip(
                prior_last_seen_ts=prior_ls,
                prior_total_score=prior_score,
                prior_leaderboard_pnl=prior_signal.get("leaderboard_pnl"),
                new_leaderboard_pnl=entry.pnl,
                now=now,
            ):
                continue
            if _needs_full_detail(
                prior_last_active_at=prior_la,
                prior_last_seen_ts=prior_ls,
                prior_total_score=prior_score,
                prior_thin_signal=prior_signal,
                rank=entry.rank,
                leaderboard_pnl=entry.pnl,
                now=now,
            ):
                detail_candidates.append(entry)

        # ── Phase 2 — budgeted full score for the selected subset only.
        # ``detail_candidates`` is already in stable wallet order (built
        # from ``ordered``); the budget slice is therefore deterministic.
        budgeted = detail_candidates[:_DISCOVERY_DETAIL_BUDGET]
        logger.info(
            "discover_leaderboard: %d/%d entries selected for full detail (budget %d)",
            len(budgeted),
            len(ordered),
            _DISCOVERY_DETAIL_BUDGET,
        )

        by_wallet = {e.wallet: e for e in budgeted}

        async def _full(wallet: str) -> None:
            await self._process_wallet(
                wallet,
                "leaderboard",
                display_name=by_wallet[wallet].username,
            )

        # L1: bounded-concurrency fan-out.  Keys are sorted so the (DB-
        # side-effecting, independent-per-wallet) work is dispatched in a
        # stable order; per-wallet upserts are individually transactional
        # so completion order does not affect persisted state.
        await _bounded_gather(sorted(by_wallet), _full)

        logger.info("discover_leaderboard: done")

    # ------------------------------------------------------------------
    # Source 2: Hot-market top holders (daily)
    # ------------------------------------------------------------------

    async def discover_hot_market_holders(self) -> None:
        """Fetch top-10 holders for each 24h-volume > $100K market (PRD §5.2).

        Datasource split (P0 — real bug fix)
        ------------------------------------
        * **Fixture path** (``isinstance(self._ds, FixtureDataSource)``):
          unchanged — ``self._ds.get_markets_by_volume(...)`` →
          ``condition_id`` list.  Byte-identical to the prior behaviour
          so every existing fixture test still passes verbatim.
        * **Live path**: the FROZEN ``LiveDataSource.get_markets_by_volume``
          targets the data-api ``GET /markets`` which is a hard ``404``
          (that endpoint does not exist on the data-api).  ``live.py`` is
          frozen, so we source the volume-ranked markets caller-side via
          :func:`fetch_hot_markets_gamma` (gamma-api) and feed the
          resulting condition ids into the SAME, verified-correct
          ``get_market_top_holders`` loop below.

        ``fetch_hot_markets_gamma`` is itself fail-soft (returns ``[]`` +
        one ``discovery_hot_markets_gamma_unavailable`` warning on any
        error), so a gamma-api outage degrades this source to an empty
        pass immediately instead of burning the frozen ``_get`` 3×
        retry/backoff — the dead source then surfaces as
        ``hot_market_holders: 0`` in the heartbeat.
        """
        logger.info("discover_hot_market_holders: start")
        condition_ids: list[str] = []
        if isinstance(self._ds, FixtureDataSource):
            markets = await self._ds.get_markets_by_volume(min_volume_24h=100_000.0, limit=200)
            condition_ids = [m.condition_id for m in markets]
        else:
            # Live path: caller-side gamma-api source (data-api
            # /markets is a hard 404; live.py is frozen).  Fail-soft —
            # an empty list here is a degraded-but-safe pass, NOT a
            # crash; warn once and skip rather than retry/backoff.
            hot = await fetch_hot_markets_gamma(min_vol=100_000.0, limit=200)
            if not hot:
                logger.warning(
                    "discovery_hot_markets_unavailable",
                    extra={
                        "reason": "gamma_markets_empty_or_unreachable",
                    },
                )
            condition_ids = [cid for cid, _row in hot]
        logger.info("discover_hot_market_holders: %d hot markets", len(condition_ids))

        # C4 (PolyHermes deep-dive): on the LIVE path only, persist each
        # hot market's metadata through the cache layer (cache -> markets
        # DB table -> remote). The first pass writes the markets table;
        # subsequent passes serve the same condition_ids from the DB /
        # in-process cache without a remote round-trip, and a RESOLVED
        # market is served forever. The FIXTURE path is deliberately left
        # untouched: a fixture is deterministic replay data, so caching it
        # is pointless and would break the byte-identical fixture-path
        # guarantee documented above. Fail-soft: the cache swallows
        # DB/remote errors, so a cache miss never blocks the scan below.
        if not isinstance(self._ds, FixtureDataSource):
            for condition_id in condition_ids:
                try:
                    await self._market_cache.get_market(condition_id)
                except Exception:  # noqa: BLE001 - cache is best-effort
                    logger.warning(
                        "discovery_market_cache_warm_failed",
                        extra={"condition_id": condition_id},
                    )

        seen: set[str] = set()
        for condition_id in condition_ids:
            try:
                holders = await self._ds.get_market_top_holders(condition_id, limit=10)
            except Exception:
                logger.exception(
                    "failed to fetch holders for market",
                    extra={"condition_id": condition_id},
                )
                continue

            for pos in holders:
                seen.add(pos.wallet)

        # L1: dedup → stable sorted order → bounded-concurrency fan-out.
        # The per-market fetch loop stays sequential (it depends on the
        # market list and dedups across markets); only the independent
        # per-wallet pipeline is parallelised.  Sorting the keys preserves
        # the deterministic processing order the prior code had (it
        # processed in first-seen order; sorting is a *stronger*
        # determinism guarantee and matches the worker-wide
        # ``sorted(...)`` discipline so fixture runs are reproducible).
        async def _full(wallet: str) -> None:
            await self._process_wallet(wallet, "hot_market_holders")

        await _bounded_gather(sorted(seen), _full)

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

        L2 windowed cursor (anti-duplication)
        -------------------------------------
        This is the hourly 24 h re-scan — the biggest source of
        duplicated work, and it is pure ">$threshold trade since cursor?"
        detection (NOT rolling stats).  So for every wallet that already
        has a persisted ``last_seen_ts`` we push ``start=last_seen_ts``
        (when it is *newer* than the 24 h trailing window) down into
        ``get_user_activity`` — the frozen datasource already supports
        ``start=``; we only pass it.  A wallet whose newest trade ts is
        ≤ its stored cursor therefore returns NO rows and is correctly
        NOT re-surfaced.  Cursors are read in ONE batched query
        (``_read_cursors``), never per-wallet.

        L1 bounded concurrency
        ----------------------
        Both the detection scan and the full re-score of matched wallets
        fan out via ``_bounded_gather`` (≤ ``_DISCOVERY_CONCURRENCY``).
        Matches are collected into a set and the full-score pass iterates
        ``sorted(new_wallets)`` so persistence order is deterministic
        under :class:`FixtureDataSource` regardless of gather completion
        order.

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

        window_start = datetime.now(tz=UTC) - timedelta(hours=_LARGE_POSITION_WINDOW_HOURS)
        # L2: one batched cursor read for the whole pool (no per-wallet
        # query).  Per wallet the effective lower bound is the LATER of
        # the 24 h trailing window and its persisted cursor — so we never
        # widen past 24 h, and a wallet already scanned up to a recent
        # cursor re-fetches only the un-seen tail (often nothing).
        cursors = await _read_cursors(wallet_pool)
        new_wallets: set[str] = set()

        async def _scan(wallet: str) -> None:
            cursor = cursors.get(wallet)
            effective_start = window_start
            if cursor is not None:
                # Cursor is INCLUSIVE-seen → scan strictly AFTER it
                # (datasource ``start=`` is ``>=``).  Take the later of
                # the 24 h window and (cursor + 1 µs) so we never widen
                # past 24 h and never re-fetch the already-seen boundary
                # trade.
                cursor_exclusive = cursor + _CURSOR_EXCLUSIVE_EPSILON
                if cursor_exclusive > effective_start:
                    effective_start = cursor_exclusive
            try:
                # limit=None → fetch the FULL window (datasource
                # paginates internally).  No arbitrary page cap can
                # truncate the wallet's largest fill out of the scan.
                activity = await self._ds.get_user_activity(
                    wallet,
                    start=effective_start,
                    activity_type="TRADE",
                    limit=None,
                )
            except Exception:
                logger.warning(
                    "large-position scan: activity fetch failed",
                    extra={"wallet": wallet},
                )
                return

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
                # Single-threaded asyncio event loop → set.add across
                # gathered coroutines is safe (no true parallelism).
                new_wallets.add(wallet)

        # L1: bounded-concurrency detection scan.  Keys sorted only for a
        # deterministic dispatch order; ``new_wallets`` is re-sorted below
        # before any persistence so the result is order-independent.
        await _bounded_gather(sorted(wallet_pool), _scan)

        logger.info(
            "discover_large_new_positions: %d wallets with large new positions",
            len(new_wallets),
        )

        # Determinism: sort the matched set BEFORE the side-effecting
        # full-score pass so persisted order is reproducible (L1).  Each
        # ``_process_wallet`` advances the wallet's ``last_seen_ts``
        # cursor in its own txn (L2) so the NEXT hourly run skips what
        # this run just saw.
        async def _full(wallet: str) -> None:
            await self._process_wallet(wallet, "large_new_positions")

        await _bounded_gather(sorted(new_wallets), _full)

        logger.info("discover_large_new_positions: done")

    # ------------------------------------------------------------------
    # Orchestration helpers
    # ------------------------------------------------------------------

    def _log_cycle_duration(self, cycle: str, seconds: float) -> None:
        """Emit the ``discovery_cycle_seconds`` structured log line.

        PRD §7.1 — a full recompute should complete in < 30 min.
        STRICTLY best-effort: the body is wrapped so a logging / len()
        failure can never propagate into the discovery pass.  No event is
        emitted (the shared ``EventType`` enum is frozen) — LOG field only.
        ``candidate_count`` is the number of distinct candidate wallets
        accumulated so far (``self._known_wallets``).
        """
        try:
            if seconds < 0:
                # Sentinel from a misbehaving monotonic clock — skip.
                return
            logger.info(
                "discovery_cycle_complete",
                extra={
                    "cycle": cycle,
                    "discovery_cycle_seconds": round(seconds, 3),
                    "discovery_cycle_target_seconds": (DISCOVERY_CYCLE_TARGET_SECONDS),
                    "candidate_count": len(self._known_wallets),
                },
            )
        except Exception as exc:  # noqa: BLE001 - observability best-effort
            logger.debug(
                "discovery_cycle_log_failed_continuing",
                extra={"error": str(exc)},
            )

    async def run_daily(self) -> None:
        """Run the daily discovery pipeline (sources 1 + 2, PRD §5.2)."""
        logger.info("run_daily: start")
        with perf_timer() as elapsed:
            await self.discover_leaderboard()
            await self.discover_hot_market_holders()
        logger.info("run_daily: complete")
        self._log_cycle_duration("daily", elapsed[0])

    async def run_hourly(self) -> None:
        """Run the hourly discovery pipeline (source 3, PRD §5.2)."""
        logger.info("run_hourly: start")
        with perf_timer() as elapsed:
            await self.discover_large_new_positions()
        logger.info("run_hourly: complete")
        self._log_cycle_duration("hourly", elapsed[0])
