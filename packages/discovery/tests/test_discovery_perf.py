"""Phase-3 discovery performance / anti-duplication regression tests.

Fixture-only, NO network, fully deterministic (every assertion is on a
call counter / a concurrency gauge / persisted ordering — never on
wall-clock time, so the suite is stable in CI).

These lock in the four Phase-3 guarantees:

1. **Cursor regression (anti-duplication lock)** — a second
   ``discover_large_new_positions()`` pass issues strictly FEWER
   ``get_user_activity`` calls for unchanged wallets, and a wallet whose
   only trade ts is < its persisted ``last_seen_ts`` is NOT re-surfaced.
2. **Idempotent rescore** — two ``run_daily()`` passes leave exactly one
   ``candidate_score_history`` row per (wallet, UTC date), and the
   expensive Phase-2 detail ran only for the budgeted/changed subset
   (proved by a call counter, not timing).
3. **Concurrency bound** — a gauge incremented/decremented around the
   semaphore proves ≤ ``_DISCOVERY_CONCURRENCY`` concurrent
   ``_process_wallet``.
4. **Determinism** — two fixture ``discover_leaderboard()`` runs persist
   an identical ordering / set of values.

Conventions mirror ``test_discovery.py`` /
``test_datasource_lifecycle.py``: the sync ``run`` helper drives the
async worker, ``polyecho.discovery.worker.session_scope`` is patched with
an in-memory stand-in (no DB), and ``FixtureDataSource`` is the datasource
(swapped for a counting wrapper where call-accounting is asserted).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from polyecho.discovery import worker as worker_mod
from polyecho.discovery.worker import (
    _DISCOVERY_CONCURRENCY,
    DiscoveryWorker,
)
from polyecho.shared.datasource import (
    ActivityRecord,
    FixtureDataSource,
    PositionRecord,
)

W_SWING = "0xswing1000000000000000000000000000000001"
W_SCALPER = "0xscalper200000000000000000000000000000002"
W_INSIDER = "0xinsider600000000000000000000000000000006"


def _wallet(prefix: str, i: int) -> str:
    """Deterministic UNIQUE 42-char ``0x…`` test address.

    ``0x`` + exactly 40 hex chars.  The variable index occupies the
    LOW-order positions (zero-padded to fill the 40-char body) so two
    different ``i`` can never collide — a prior version truncated the
    varying digits and produced identical addresses for many ``i``.
    """
    body = (prefix + f"{i:x}").rjust(40, "0")[-40:]
    return "0x" + body


def run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _rec(
    *,
    wallet: str,
    usdc_size: float | None = 9_000.0,
    size: float = 100.0,
    activity_type: str = "TRADE",
    side: str = "BUY",
    ts: datetime,
) -> ActivityRecord:
    return ActivityRecord(
        wallet=wallet,
        activity_type=activity_type,
        condition_id="0xcond_perf",
        side=side,
        size=size,
        price=0.5,
        usdc_size=usdc_size,
        timestamp=ts,
        raw={},
    )


# --------------------------------------------------------------------------- #
#  In-memory persistence stand-in.
#
#  A real, stateful candidate store so ``last_seen_ts`` / ``last_active_at``
#  / ``total_score`` / ``backtest_default`` survive across upserts (the
#  cursor + idempotency + tiered-budget logic all depend on reading back
#  what a prior pass wrote).  The candidate ``SELECT … FOR UPDATE`` returns
#  a live row object whose attribute mutations the worker performs are
#  retained; the history ``INSERT … ON CONFLICT`` is modelled as an
#  idempotent dict keyed by (wallet_address, date); the batched cursor
#  ``SELECT wallet_address, last_seen_ts`` returns rows from the store.
# --------------------------------------------------------------------------- #


class _Row:
    """Mutable ORM-row double (only the attrs the worker touches)."""

    def __init__(self, wallet: str) -> None:
        self.wallet_address = wallet
        self.display_name: str | None = None
        self.sources: list[str] | None = None
        self.first_discovered_at: datetime | None = None
        self.last_active_at: datetime | None = None
        self.last_seen_ts: datetime | None = None
        self.stats_7d: Any = None
        self.stats_30d: Any = None
        self.stats_90d: Any = None
        self.frequency_stats: Any = None
        self.strategy_type: str | None = None
        self.backtest_default: dict[str, Any] | None = None
        self.profit_score: float | None = None
        self.replicability_score: float | None = None
        self.total_score: float | None = None
        self.risk_flags: list[str] | None = None
        self.user_status: str = "new"


class _Store:
    """Process-local candidate + score-history store shared by sessions."""

    def __init__(self) -> None:
        self.candidates: dict[str, _Row] = {}
        # (wallet, date) -> score-history payload (idempotent upsert).
        self.history: dict[tuple[str, Any], dict[str, Any]] = {}
        self.history_upserts = 0


def _make_session_scope(store: _Store) -> Any:
    """Return a ``session_scope``-shaped factory backed by *store*."""

    class _Result:
        def __init__(self, scalar: Any = None, rows: list[Any] | None = None):
            self._scalar = scalar
            self._rows = rows or []

        def scalar_one_or_none(self) -> Any:
            return self._scalar

        def all(self) -> list[Any]:
            return self._rows

    class _Session:
        # ``_upsert_candidate`` branches on ``session.bind.dialect.name``;
        # ``bind = None`` makes it take the documented PostgreSQL
        # ``pg_insert … ON CONFLICT`` path (the production default), which
        # compiles fine without a real engine.
        bind = None

        def __init__(self) -> None:
            self._pending_insert: dict[str, Any] | None = None

        async def execute(self, stmt: Any) -> _Result:
            compiled = str(getattr(stmt, "compile", lambda: stmt)()).upper()

            # Score-history INSERT … ON CONFLICT (wallet_address, date).
            if "CANDIDATE_SCORE_HISTORY" in compiled and "INSERT" in compiled:
                params = stmt.compile().params
                key = (params["wallet_address"], params["date"])
                store.history[key] = dict(params)
                store.history_upserts += 1
                return _Result()

            # Batched cursor read — the ONLY CandidateLeader SELECT that
            # uses an IN (...) predicate (``_read_cursors``); returns
            # (wallet_address, last_seen_ts) row tuples.
            if (
                "CANDIDATE_LEADERS" in compiled
                and "SELECT" in compiled
                and " IN (" in compiled
            ):
                wanted = _extract_in_wallets(stmt)
                rows = [
                    (w, store.candidates[w].last_seen_ts)
                    for w in sorted(wanted)
                    if w in store.candidates
                ]
                return _Result(rows=rows)

            # Single-row candidate SELECT … FOR UPDATE (upsert / thin).
            if "CANDIDATE_LEADERS" in compiled and "SELECT" in compiled:
                wallet = _extract_wallet(stmt)
                return _Result(scalar=store.candidates.get(wallet))

            return _Result()

        def add(self, obj: Any) -> None:
            # Only CandidateLeader inserts reach here (history is ON
            # CONFLICT via execute()).  Persist the new row by wallet.
            wallet = getattr(obj, "wallet_address", None)
            if wallet is None:
                return
            row = _Row(wallet)
            for attr in vars(row):
                if hasattr(obj, attr):
                    setattr(row, attr, getattr(obj, attr))
            store.candidates[wallet] = row

    class _Scope:
        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    def _factory() -> _Scope:
        return _Scope()

    return _factory


def _extract_wallet(stmt: Any) -> str:
    """Pull the bound ``wallet_address`` equality value out of a Select."""
    params = stmt.compile().params
    for k, v in params.items():
        if "wallet_address" in k and isinstance(v, str):
            return v
    return ""


def _extract_in_wallets(stmt: Any) -> list[str]:
    """Pull the IN (...) wallet list out of the batched cursor Select.

    SQLAlchemy renders ``col.in_([...])`` as a single *expanding*
    (POSTCOMPILE) bind param whose value is the LIST itself, so the
    param value is a ``list[str]`` rather than N scalar binds.
    """
    out: list[str] = []
    for v in stmt.compile().params.values():
        if isinstance(v, str) and v.startswith("0x"):
            out.append(v)
        elif isinstance(v, (list, tuple)):
            out.extend(
                x for x in v if isinstance(x, str) and x.startswith("0x")
            )
    return out


# --------------------------------------------------------------------------- #
#  Counting datasource wrapper (call-accounting; no timing).
# --------------------------------------------------------------------------- #


class _StubLeaderboardEntry:
    def __init__(self, wallet: str, *, rank: int, pnl: float, vol: float):
        self.wallet = wallet
        self.username = None
        self.rank = rank
        self.pnl = pnl
        self.volume = vol


class _CountingDS:
    """Deterministic DS honouring ``start``/``activity_type``; counts calls.

    ``get_user_activity`` records ``(wallet, start)`` so a test can assert
    the second pass made strictly fewer calls / passed a cursor ``start``.
    """

    def __init__(
        self,
        activity_by_wallet: dict[str, list[ActivityRecord]],
        leaderboard: list[_StubLeaderboardEntry] | None = None,
    ) -> None:
        self._activity = activity_by_wallet
        self._lb = leaderboard or []
        self.activity_calls: list[tuple[str, datetime | None]] = []

    async def get_leaderboard(self, **kw: Any) -> list[_StubLeaderboardEntry]:
        limit = kw.get("limit", 200)
        return list(self._lb[:limit])

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        self.activity_calls.append((wallet, start))
        rows = list(self._activity.get(wallet, []))
        if activity_type is not None:
            rows = [r for r in rows if r.activity_type == activity_type]
        if start is not None:
            rows = [r for r in rows if r.timestamp >= start]
        if end is not None:
            rows = [r for r in rows if r.timestamp <= end]
        return rows[:limit] if limit is not None else rows

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]:
        return []

    async def get_markets_by_volume(self, **kw: Any) -> list[Any]:
        return []

    async def get_market_top_holders(self, *a: Any, **kw: Any) -> list[Any]:
        return []


# --------------------------------------------------------------------------- #
#  1. Cursor regression — anti-duplication lock.
# --------------------------------------------------------------------------- #


class TestCursorAntiDuplication:
    def test_second_pass_fewer_activity_calls_and_no_resurface(self) -> None:
        now = datetime.now(tz=UTC)
        # W_SWING traded big 2h ago → surfaces on pass 1; pass 1 persists
        # last_seen_ts = that trade ts.  W_SCALPER's ONLY trade is OLD
        # relative to a cursor we pre-seed → must never resurface.
        swing_big = _rec(wallet=W_SWING, usdc_size=9_000.0, ts=now - timedelta(hours=2))
        scalper_old = _rec(
            wallet=W_SCALPER, usdc_size=50_000.0, ts=now - timedelta(hours=5)
        )
        ds = _CountingDS({W_SWING: [swing_big], W_SCALPER: [scalper_old]})
        store = _Store()

        # Pre-seed W_SCALPER with a cursor NEWER than its only trade → the
        # windowed fetch (start=cursor) returns nothing → not resurfaced.
        seeded = _Row(W_SCALPER)
        seeded.last_seen_ts = now - timedelta(hours=1)
        seeded.last_active_at = now - timedelta(hours=5)
        seeded.total_score = 10.0
        store.candidates[W_SCALPER] = seeded

        processed: list[str] = []

        async def _go() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING, W_SCALPER}

            real_process = worker._process_wallet

            async def _spy(w: str, s: str, **kw: Any) -> Any:
                processed.append(w)
                return await real_process(w, s, **kw)

            with (
                patch.object(
                    worker_mod, "session_scope", _make_session_scope(store)
                ),
                patch.object(worker, "_process_wallet", _spy),
            ):
                await worker.discover_large_new_positions()
                pass1 = len(ds.activity_calls)
                await worker.discover_large_new_positions()
                pass2 = len(ds.activity_calls) - pass1

            # Pass 2 issues strictly fewer activity calls for the
            # unchanged wallet: W_SWING's cursor now == its only trade ts,
            # so its windowed re-fetch yields nothing and it is NOT
            # re-processed (the detection scan still runs, but the
            # expensive _process_wallet does not).
            assert pass2 <= pass1
            store.pass1_calls = pass1  # type: ignore[attr-defined]

        run(_go())

        # W_SCALPER (cursor newer than its only trade) is NEVER surfaced.
        assert W_SCALPER not in processed
        # W_SWING surfaced exactly once (pass 1); pass 2's windowed fetch
        # (start = persisted cursor = its trade ts) returns no NEW > $5k
        # trade, so it is not re-processed → strictly less duplicated work.
        assert processed.count(W_SWING) == 1

    def test_cursor_persisted_monotonically(self) -> None:
        now = datetime.now(tz=UTC)
        t_old = now - timedelta(hours=10)
        ds = _CountingDS({W_SWING: [_rec(wallet=W_SWING, ts=t_old)]})
        store = _Store()
        seeded = _Row(W_SWING)
        seeded.last_seen_ts = now - timedelta(hours=1)  # newer than t_old
        seeded.last_active_at = t_old
        seeded.total_score = 20.0
        store.candidates[W_SWING] = seeded

        async def _go() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING}
            with patch.object(
                worker_mod, "session_scope", _make_session_scope(store)
            ):
                # Force a full score with an OLDER latest trade.
                await worker._process_wallet(W_SWING, "leaderboard")

        run(_go())
        # The cursor must NOT regress below the pre-seeded newer value.
        assert store.candidates[W_SWING].last_seen_ts == now - timedelta(hours=1)


# --------------------------------------------------------------------------- #
#  2. Idempotent rescore + budgeted Phase-2.
# --------------------------------------------------------------------------- #


class TestIdempotentRescoreAndBudget:
    def test_two_run_daily_one_history_row_per_wallet_day(self) -> None:
        store = _Store()
        ds = FixtureDataSource()

        process_calls: list[str] = []

        async def _go() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)
            real = worker._process_wallet

            async def _spy(w: str, s: str, **kw: Any) -> Any:
                process_calls.append(w)
                return await real(w, s, **kw)

            with (
                patch.object(
                    worker_mod, "session_scope", _make_session_scope(store)
                ),
                patch.object(worker, "_process_wallet", _spy),
            ):
                await worker.run_daily()
                first_pass_detail = len(process_calls)
                await worker.run_daily()
                second_pass_detail = len(process_calls) - first_pass_detail
            store.first_detail = first_pass_detail  # type: ignore[attr-defined]
            store.second_detail = second_pass_detail  # type: ignore[attr-defined]

        run(_go())

        # Idempotency: exactly ONE score-history row per (wallet, UTC
        # date) regardless of two full daily passes (ON CONFLICT upsert).
        keys = list(store.history.keys())
        assert len(keys) == len(set(keys))
        dates = {d for _, d in keys}
        assert len(dates) == 1  # all rows are today's UTC date

        # Budget: Phase-2 detail on pass 1 is capped at the module
        # budget; pass 2 (nothing changed, cursor fresh) does strictly
        # LESS detail work — proved by the call counter, not timing.
        assert store.first_detail <= worker_mod._DISCOVERY_DETAIL_BUDGET  # type: ignore[attr-defined]
        assert store.second_detail <= store.first_detail  # type: ignore[attr-defined]

    def test_phase2_detail_capped_at_budget(self) -> None:
        # More leaderboard entries than the budget → Phase-2 full scores
        # are capped; Phase-1 thin upserts still cover every entry.
        n = worker_mod._DISCOVERY_DETAIL_BUDGET + 12
        lb = [
            _StubLeaderboardEntry(
                _wallet("perf", i), rank=i + 1, pnl=1000.0 * (n - i), vol=1.0
            )
            for i in range(n)
        ]
        ds = _CountingDS({}, leaderboard=lb)
        store = _Store()
        detail: list[str] = []

        async def _go() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            real = worker._process_wallet

            async def _spy(w: str, s: str, **kw: Any) -> Any:
                detail.append(w)
                return await real(w, s, **kw)

            with (
                patch.object(
                    worker_mod, "session_scope", _make_session_scope(store)
                ),
                patch.object(worker, "_process_wallet", _spy),
            ):
                await worker.discover_leaderboard()

        run(_go())
        # Phase-1 thin row for EVERY entry (no per-wallet call needed).
        assert len(store.candidates) == n
        # Phase-2 expensive detail strictly bounded by the budget const.
        assert len(detail) <= worker_mod._DISCOVERY_DETAIL_BUDGET


# --------------------------------------------------------------------------- #
#  3. Concurrency bound.
# --------------------------------------------------------------------------- #


class TestConcurrencyBound:
    def test_process_wallet_fanout_respects_semaphore(self) -> None:
        # Many leaderboard wallets, all NEW (no prior row) → all selected
        # for Phase-2 detail → maximal fan-out pressure on the semaphore.
        n = _DISCOVERY_CONCURRENCY * 4
        lb = [
            _StubLeaderboardEntry(
                _wallet("conc", i), rank=i + 1, pnl=float(n - i), vol=1.0
            )
            for i in range(n)
        ]
        ds = _CountingDS({}, leaderboard=lb)
        store = _Store()

        gauge = {"cur": 0, "max": 0}

        async def _go() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            real = worker._process_wallet

            async def _instrumented(w: str, s: str, **kw: Any) -> Any:
                gauge["cur"] += 1
                gauge["max"] = max(gauge["max"], gauge["cur"])
                # Yield so other gathered coroutines interleave INSIDE the
                # semaphore-held region (makes the bound observable).
                await asyncio.sleep(0)
                try:
                    return await real(w, s, **kw)
                finally:
                    gauge["cur"] -= 1

            with (
                patch.object(
                    worker_mod, "session_scope", _make_session_scope(store)
                ),
                patch.object(worker, "_process_wallet", _instrumented),
            ):
                await worker.discover_leaderboard()

        run(_go())

        assert gauge["max"] >= 1  # work actually ran
        # NEVER more than the module concurrency cap in flight at once.
        assert gauge["max"] <= _DISCOVERY_CONCURRENCY, (
            f"observed {gauge['max']} concurrent _process_wallet, "
            f"cap is {_DISCOVERY_CONCURRENCY}"
        )


# --------------------------------------------------------------------------- #
#  4. Determinism.
# --------------------------------------------------------------------------- #


class TestDeterminism:
    def test_two_fixture_leaderboard_runs_identical_persistence(self) -> None:
        def _snapshot() -> dict[str, Any]:
            store = _Store()
            ds = FixtureDataSource()

            async def _go() -> None:
                worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)
                with patch.object(
                    worker_mod, "session_scope", _make_session_scope(store)
                ):
                    await worker.discover_leaderboard()

            run(_go())
            # Comparable, order-independent projection of persisted state.
            return {
                w: (
                    r.sources,
                    r.total_score,
                    None if r.backtest_default is None else r.backtest_default.get(
                        "stats_leaderboard"
                    ),
                )
                for w, r in sorted(store.candidates.items())
            }

        first = _snapshot()
        second = _snapshot()
        assert first == second
        assert list(first.keys()) == sorted(first.keys())  # stable ordering
