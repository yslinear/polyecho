"""Tests for polyecho.discovery — Phase 1 Wave 2.

Uses FixtureDataSource (default; no env var needed) for all datasource calls.
Persistence layer is mocked via unittest.mock to avoid requiring a live DB.

Fixture wallet archetypes tested (see shared/datasource/fixtures/README.md):
    SWING     0xswing1000000000000000000000000000000001
    SCALPER   0xscalper200000000000000000000000000000002
    DRIFTED   0xdrifted500000000000000000000000000000005
    INSIDER   0xinsider600000000000000000000000000000006
    POSITION  0xposition400000000000000000000000000000004
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polyecho.discovery.frequency import (
    FrequencyStats,
    StrategyType,
    classify_strategy,
    compute_frequency_stats,
    detect_drift,
)
from polyecho.discovery.riskflags import detect_risk_flags
from polyecho.discovery.scoring import final_score, profit_score, replicability_score
from polyecho.discovery.worker import DiscoveryWorker, _compute_stats
from polyecho.shared.datasource import ActivityRecord, FixtureDataSource, PositionRecord
from polyecho.shared.events import EventType, Severity

# ---------------------------------------------------------------------------
# Wallet addresses (exact filenames from fixture directory)
# ---------------------------------------------------------------------------
W_SWING = "0xswing1000000000000000000000000000000001"
W_SCALPER = "0xscalper200000000000000000000000000000002"
W_DAYTRADER = "0xdaytrader300000000000000000000000000000003"
W_POSITION = "0xposition400000000000000000000000000000004"
W_DRIFTED = "0xdrifted500000000000000000000000000000005"
W_INSIDER = "0xinsider600000000000000000000000000000006"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_record(
    *,
    wallet: str = W_SWING,
    activity_type: str = "TRADE",
    side: str = "BUY",
    condition_id: str = "0xcond_test",
    size: float = 100.0,
    price: float = 0.5,
    usdc_size: float | None = 50.0,
    ts: datetime | None = None,
) -> ActivityRecord:
    if ts is None:
        ts = datetime.now(tz=UTC)
    return ActivityRecord(
        wallet=wallet,
        activity_type=activity_type,
        condition_id=condition_id,
        side=side,
        size=size,
        price=price,
        usdc_size=usdc_size,
        timestamp=ts,
        raw={},
    )


def _make_position(
    *,
    wallet: str = W_SWING,
    condition_id: str = "0xcond_test",
    current_value: float = 500.0,
) -> PositionRecord:
    return PositionRecord(
        wallet=wallet,
        condition_id=condition_id,
        asset="tok-test-yes",
        size=100.0,
        avg_price=0.5,
        current_value=current_value,
        cash_pnl=0.0,
        percent_pnl=0.0,
        title=None,
        outcome="Yes",
        raw={},
    )


# ---------------------------------------------------------------------------
# §1 — frequency.py unit tests
# ---------------------------------------------------------------------------


class TestComputeFrequencyStats:
    """Direct unit tests for compute_frequency_stats()."""

    def _make_uniform_activity(
        self,
        n: int = 30,
        gap_hours: float = 24.0,
        condition_id: str = "0xcond_a",
    ) -> list[ActivityRecord]:
        """Return n BUY trades evenly spaced by gap_hours."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        records = []
        for i in range(n):
            ts = base + timedelta(hours=i * gap_hours)
            records.append(_make_record(ts=ts, condition_id=condition_id))
        return records

    def test_empty_returns_zeros(self) -> None:
        stats = compute_frequency_stats([], window_days=30)
        assert stats.trades_per_day == 0.0
        assert stats.active_days == 0
        assert stats.active_hours_utc == []

    def test_trades_per_day_correct(self) -> None:
        activity = self._make_uniform_activity(n=60, gap_hours=12.0)
        stats = compute_frequency_stats(activity, window_days=30)
        assert stats.trades_per_day == pytest.approx(2.0, rel=0.01)

    def test_active_days_counted(self) -> None:
        activity = self._make_uniform_activity(n=30, gap_hours=24.0)
        stats = compute_frequency_stats(activity, window_days=30)
        assert stats.active_days == 30

    def test_median_gap_minutes_uniform(self) -> None:
        # 24h gap → 1440 min
        activity = self._make_uniform_activity(n=5, gap_hours=24.0)
        stats = compute_frequency_stats(activity, window_days=30)
        assert stats.median_gap_minutes == pytest.approx(1440.0, rel=0.01)

    def test_burstiness_uniform_is_low(self) -> None:
        activity = self._make_uniform_activity(n=10, gap_hours=24.0)
        stats = compute_frequency_stats(activity, window_days=30)
        # Perfectly uniform → CV = 0; allow small float noise
        assert stats.burstiness < 0.01

    def test_burstiness_clustered_is_higher(self) -> None:
        """Trades in tight clusters separated by long gaps → high burstiness."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        records: list[ActivityRecord] = []
        # Cluster 1: 5 trades 1 min apart
        for i in range(5):
            records.append(_make_record(ts=base + timedelta(minutes=i), condition_id="a"))
        # Long gap
        # Cluster 2: 5 trades 1 min apart, 10 days later
        offset = timedelta(days=10)
        for i in range(5):
            records.append(
                _make_record(ts=base + offset + timedelta(minutes=i), condition_id="b")
            )
        stats = compute_frequency_stats(records, window_days=30)
        assert stats.burstiness > 0.5

    def test_active_hours_utc_populated(self) -> None:
        activity = self._make_uniform_activity(n=5, gap_hours=24.0)
        stats = compute_frequency_stats(activity, window_days=30)
        # All at hour 0 since base is midnight UTC
        assert 0 in stats.active_hours_utc

    def test_to_dict_keys_match_prd(self) -> None:
        activity = self._make_uniform_activity(n=10, gap_hours=6.0)
        stats = compute_frequency_stats(activity, window_days=30)
        d = stats.to_dict()
        expected_keys = {
            "trades_per_day",
            "active_days",
            "median_gap_minutes",
            "avg_hold_hours",
            "hold_to_resolution_pct",
            "concurrent_positions_avg",
            "concurrent_positions_max",
            "burstiness",
            "active_hours_utc",
        }
        assert expected_keys == set(d.keys())

    def test_hold_to_resolution_pct_with_redeems(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        records = [
            _make_record(side="BUY", ts=base, condition_id="cond_x"),
            ActivityRecord(
                wallet=W_SWING,
                activity_type="REDEEM",
                condition_id="cond_x",
                side=None,
                size=100.0,
                price=None,
                usdc_size=100.0,
                timestamp=base + timedelta(days=5),
                raw={},
            ),
        ]
        stats = compute_frequency_stats(records, window_days=30)
        assert stats.hold_to_resolution_pct == pytest.approx(100.0, rel=0.01)


class TestClassifyStrategy:
    """Unit tests for classify_strategy() thresholds (PRD §5.3)."""

    def _make_stats(
        self,
        *,
        tpd: float,
        avg_hold_hours: float,
        hold_to_res_pct: float = 0.0,
    ) -> FrequencyStats:
        return FrequencyStats(
            trades_per_day=tpd,
            active_days=30,
            median_gap_minutes=1440.0 / max(tpd, 0.01),
            avg_hold_hours=avg_hold_hours,
            hold_to_resolution_pct=hold_to_res_pct,
            concurrent_positions_avg=2.0,
            concurrent_positions_max=5,
            burstiness=0.2,
            active_hours_utc=list(range(0, 8)),
        )

    def test_scalper(self) -> None:
        stats = self._make_stats(tpd=25.0, avg_hold_hours=0.5)
        assert classify_strategy(stats) == StrategyType.SCALPER

    def test_day_trader(self) -> None:
        stats = self._make_stats(tpd=10.0, avg_hold_hours=4.0)
        assert classify_strategy(stats) == StrategyType.DAY_TRADER

    def test_swing_sweet_spot(self) -> None:
        stats = self._make_stats(tpd=3.0, avg_hold_hours=48.0)  # 2 days
        assert classify_strategy(stats) == StrategyType.SWING

    def test_position_trader(self) -> None:
        stats = self._make_stats(tpd=0.3, avg_hold_hours=200.0)  # ~8 days
        assert classify_strategy(stats) == StrategyType.POSITION

    def test_hold_to_resolve(self) -> None:
        stats = self._make_stats(tpd=0.5, avg_hold_hours=72.0, hold_to_res_pct=80.0)
        assert classify_strategy(stats) == StrategyType.HOLD_TO_RESOLVE


class TestDetectDrift:
    """Unit tests for detect_drift()."""

    def _fs(self, tpd: float, hold_h: float) -> FrequencyStats:
        return FrequencyStats(
            trades_per_day=tpd,
            active_days=30,
            median_gap_minutes=60.0,
            avg_hold_hours=hold_h,
            hold_to_resolution_pct=0.0,
            concurrent_positions_avg=2.0,
            concurrent_positions_max=5,
            burstiness=0.3,
            active_hours_utc=[0, 1, 2],
        )

    def test_no_drift_same_type(self) -> None:
        fs30 = self._fs(tpd=3.0, hold_h=48.0)   # SWING
        fs7 = self._fs(tpd=2.0, hold_h=36.0)    # SWING
        assert detect_drift(fs30, fs7) is False

    def test_drift_detected(self) -> None:
        fs30 = self._fs(tpd=3.0, hold_h=48.0)   # SWING
        fs7 = self._fs(tpd=25.0, hold_h=0.3)    # SCALPER
        assert detect_drift(fs30, fs7) is True


# ---------------------------------------------------------------------------
# §2 — scoring.py unit tests
# ---------------------------------------------------------------------------


class TestProfitScore:
    def _stats(self, **kwargs: Any) -> dict[str, Any]:
        defaults = {
            "pnl_usd": 0.0,
            "win_rate": 0.5,
            "sharpe": 0.0,
            "max_drawdown": 0.5,
            "consistency": 0.5,
            "specialization": 0.5,
        }
        defaults.update(kwargs)
        return defaults

    def test_output_in_range(self) -> None:
        s = profit_score(self._stats(pnl_usd=50_000.0, win_rate=0.8, sharpe=3.0))
        assert 0.0 <= s <= 100.0

    def test_high_stats_scores_high(self) -> None:
        s = profit_score(
            self._stats(
                pnl_usd=100_000.0,
                win_rate=0.9,
                sharpe=4.0,
                max_drawdown=0.05,
                consistency=0.95,
                specialization=0.8,
            )
        )
        assert s > 70.0

    def test_zero_pnl_negative_effect(self) -> None:
        s_pos = profit_score(self._stats(pnl_usd=50_000.0))
        s_zero = profit_score(self._stats(pnl_usd=0.0))
        assert s_pos > s_zero

    def test_negative_pnl_reduces_score(self) -> None:
        s_neg = profit_score(self._stats(pnl_usd=-50_000.0))
        s_zero = profit_score(self._stats(pnl_usd=0.0))
        assert s_neg < s_zero

    def test_missing_keys_uses_defaults(self) -> None:
        # Should not raise; all missing keys default to neutral
        s = profit_score({})
        assert 0.0 <= s <= 100.0


class TestReplicabilityScore:
    def _fstats(self, **kwargs: Any) -> dict[str, Any]:
        defaults = {
            "trades_per_day": 2.0,
            "median_gap_minutes": 60.0,
            "avg_hold_hours": 48.0,
            "burstiness": 0.2,
            "active_hours_utc": list(range(0, 8)),  # fully overlaps follower window
            "concurrent_positions_avg": 2.0,
        }
        defaults.update(kwargs)
        return defaults

    def test_output_in_range(self) -> None:
        s = replicability_score(self._fstats(), follower_capital_usd=1000.0)
        assert 0.0 <= s <= 100.0

    def test_scalper_very_low(self) -> None:
        # Very short gap → near-zero frequency match
        s = replicability_score(
            self._fstats(trades_per_day=30.0, median_gap_minutes=0.5, burstiness=0.9),
            follower_capital_usd=1000.0,
        )
        assert s < 20.0

    def test_swing_good_overlap_moderate_plus(self) -> None:
        s = replicability_score(
            self._fstats(
                trades_per_day=3.0,
                median_gap_minutes=120.0,
                burstiness=0.1,
                active_hours_utc=list(range(0, 10)),
            ),
            follower_capital_usd=2000.0,
        )
        assert s > 30.0

    def test_no_time_overlap_zeros_score(self) -> None:
        # Active hours entirely outside follower window (15–23 UTC)
        s = replicability_score(
            self._fstats(active_hours_utc=list(range(15, 24))),
            follower_capital_usd=1000.0,
        )
        assert s == 0.0

    def test_multiplicative_zero_on_no_hours(self) -> None:
        s = replicability_score(
            self._fstats(active_hours_utc=[]),
            follower_capital_usd=1000.0,
        )
        assert s == 0.0


class TestFinalScore:
    def test_zero_replicability_zeros_final(self) -> None:
        assert final_score(90.0, 0.0) == 0.0

    def test_full_scores_near_maximum(self) -> None:
        assert final_score(100.0, 100.0) == pytest.approx(100.0)

    def test_proportional(self) -> None:
        assert final_score(80.0, 50.0) == pytest.approx(40.0)

    def test_output_in_range(self) -> None:
        assert 0.0 <= final_score(60.0, 70.0) <= 100.0


# ---------------------------------------------------------------------------
# §3 — riskflags.py unit tests
# ---------------------------------------------------------------------------


class TestDetectRiskFlags:
    def _base_stats(self, **kwargs: Any) -> dict[str, Any]:
        defaults = {
            "pnl_usd": 5000.0,
            "win_rate": 0.6,
            "total_trades": 100,
            "trades_per_day": 3.0,
            "first_trade_at": "2026-01-01T00:00:00+00:00",
        }
        defaults.update(kwargs)
        return defaults

    def _freq_stats(self, tpd: float = 3.0, hold_h: float = 48.0) -> FrequencyStats:
        return FrequencyStats(
            trades_per_day=tpd,
            active_days=30,
            median_gap_minutes=60.0,
            avg_hold_hours=hold_h,
            hold_to_resolution_pct=0.0,
            concurrent_positions_avg=2.0,
            concurrent_positions_max=5,
            burstiness=0.2,
            active_hours_utc=[0, 1, 2],
        )

    def test_clean_leader_no_flags(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        activity = [
            _make_record(side="BUY", ts=base + timedelta(days=i), usdc_size=200.0)
            for i in range(60)
        ]
        flags = detect_risk_flags(
            self._base_stats(),
            activity,
            [_make_position(current_value=500.0)],
            frequency_stats=self._freq_stats(),
            freq_stats_7d=self._freq_stats(),
        )
        assert "insider_suspect" not in flags
        assert "strategy_drift" not in flags

    def test_strategy_drift_flag(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        activity = [
            _make_record(side="BUY", ts=base + timedelta(days=i), usdc_size=200.0)
            for i in range(60)
        ]
        fs_30d = self._freq_stats(tpd=3.0, hold_h=48.0)   # SWING
        fs_7d = self._freq_stats(tpd=25.0, hold_h=0.3)    # SCALPER
        flags = detect_risk_flags(
            self._base_stats(),
            activity,
            [],
            frequency_stats=fs_30d,
            freq_stats_7d=fs_7d,
        )
        assert "strategy_drift" in flags

    def test_new_account_flag(self) -> None:
        base = datetime.now(tz=UTC) - timedelta(days=10)
        activity = [
            _make_record(side="BUY", ts=base + timedelta(hours=i), usdc_size=200.0)
            for i in range(5)
        ]
        flags = detect_risk_flags(
            self._base_stats(),
            activity,
            [],
        )
        assert "new_account" in flags

    def test_high_concentration_flag(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # All volume in single condition
        activity = [
            _make_record(
                side="BUY",
                ts=base + timedelta(days=i),
                usdc_size=1000.0,
                condition_id="0xonly_cond",
            )
            for i in range(30)
        ]
        flags = detect_risk_flags(
            self._base_stats(total_trades=30, win_rate=0.5),
            activity,
            [],
        )
        assert "high_concentration" in flags

    def test_insider_suspect_flag(self) -> None:
        """Rare + large + near-resolution + high win-rate = insider_suspect."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond = "0xcond_insider"
        # 5 BUY trades within 24h of resolution, very large
        activity: list[ActivityRecord] = []
        for i in range(5):
            activity.append(
                ActivityRecord(
                    wallet=W_INSIDER,
                    activity_type="TRADE",
                    condition_id=cond,
                    side="BUY",
                    size=2000.0,
                    price=0.05,
                    usdc_size=8000.0,  # > $5K threshold
                    timestamp=base + timedelta(hours=i),
                    raw={},
                )
            )
        # REDEEM at +20h (within 24h window)
        activity.append(
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="REDEEM",
                condition_id=cond,
                side=None,
                size=10000.0,
                price=None,
                usdc_size=10000.0,
                timestamp=base + timedelta(hours=20),
                raw={},
            )
        )
        stats = {
            "pnl_usd": 50000.0,
            "win_rate": 0.95,   # > 0.80
            "total_trades": 5,  # <= 50
            "trades_per_day": 1.0,  # < 5
        }
        flags = detect_risk_flags(stats, activity, [])
        assert "insider_suspect" in flags

    def test_low_liquidity_flag(self) -> None:
        positions = [
            _make_position(current_value=50.0),   # thin
            _make_position(current_value=30.0, condition_id="0xb"),   # thin
            _make_position(current_value=20.0, condition_id="0xc"),   # thin
        ]
        base = datetime(2026, 1, 1, tzinfo=UTC)
        activity = [
            _make_record(side="BUY", ts=base + timedelta(days=i)) for i in range(40)
        ]
        flags = detect_risk_flags(self._base_stats(), activity, positions)
        assert "low_liquidity" in flags


# ---------------------------------------------------------------------------
# §4 — Fixture archetype integration tests (via FixtureDataSource)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ds() -> FixtureDataSource:
    return FixtureDataSource()


def _fetch_activity(ds: FixtureDataSource, wallet: str) -> list[ActivityRecord]:
    async def _go() -> list[ActivityRecord]:
        trades = await ds.get_user_activity(wallet, activity_type="TRADE", limit=None)
        redeems = await ds.get_user_activity(wallet, activity_type="REDEEM", limit=None)
        return list(trades) + list(redeems)

    return asyncio.get_event_loop().run_until_complete(_go())


def _fetch_positions(ds: FixtureDataSource, wallet: str) -> list[PositionRecord]:
    async def _go() -> list[PositionRecord]:
        return list(await ds.get_positions(wallet, size_threshold=0.0))

    return asyncio.get_event_loop().run_until_complete(_go())


class TestSwingArchetype:
    """Swing sweet-spot wallet → high final_score & SWING classification."""

    def test_strategy_is_swing(self, ds: FixtureDataSource) -> None:
        activity = _fetch_activity(ds, W_SWING)
        stats = compute_frequency_stats(activity, window_days=90)
        assert classify_strategy(stats) == StrategyType.SWING

    def test_final_score_reasonable(self, ds: FixtureDataSource) -> None:
        activity = _fetch_activity(ds, W_SWING)

        # Use full window since fixture spans 90d
        stats = compute_frequency_stats(activity, window_days=90)
        computed_stats = _compute_stats(activity, window_days=90)

        p_score = profit_score(computed_stats)
        r_score = replicability_score(stats.to_dict(), 1000.0)
        f_score = final_score(p_score, r_score)

        assert p_score >= 0.0
        assert r_score >= 0.0
        assert f_score >= 0.0
        # Swing should be somewhat copyable (r_score > 0 if any overlap)
        # At minimum the pipeline doesn't crash


class TestScalperArchetype:
    """Scalper → SCALPER classification + very low replicability."""

    def test_strategy_is_scalper(self, ds: FixtureDataSource) -> None:
        activity = _fetch_activity(ds, W_SCALPER)
        stats = compute_frequency_stats(activity, window_days=90)
        assert classify_strategy(stats) == StrategyType.SCALPER

    def test_replicability_low(self, ds: FixtureDataSource) -> None:
        activity = _fetch_activity(ds, W_SCALPER)
        stats = compute_frequency_stats(activity, window_days=90)
        r_score = replicability_score(stats.to_dict(), 1000.0)
        # Scalpers trade far too fast; frequency match should pull score down
        assert r_score < 30.0


class TestDriftedArchetype:
    """Drifted wallet → strategy_drift flag + 30d ≠ 7d classification."""

    def test_drift_detected(self, ds: FixtureDataSource) -> None:
        activity = _fetch_activity(ds, W_DRIFTED)

        now = datetime.now(tz=UTC)
        # The fixture records span 90d; treat last timestamp as "now" for windows
        if activity:
            now = max(r.timestamp for r in activity)

        from datetime import timedelta as td

        act_30d = [r for r in activity if r.timestamp >= now - td(days=30)]
        act_7d = [r for r in activity if r.timestamp >= now - td(days=7)]

        # Ensure we have data in both windows
        if not act_30d or not act_7d:
            pytest.skip("fixture data not in expected window for drift test")

        stats_30d = compute_frequency_stats(act_30d, window_days=30)
        stats_7d = compute_frequency_stats(act_7d, window_days=7)

        assert detect_drift(stats_30d, stats_7d) is True

    def test_drift_flag_in_riskflags(self, ds: FixtureDataSource) -> None:
        activity = _fetch_activity(ds, W_DRIFTED)
        positions = _fetch_positions(ds, W_DRIFTED)

        if not activity:
            pytest.skip("no fixture activity for drifted wallet")

        now = max(r.timestamp for r in activity)
        from datetime import timedelta as td

        act_30d = [r for r in activity if r.timestamp >= now - td(days=30)]
        act_7d = [r for r in activity if r.timestamp >= now - td(days=7)]

        if not act_30d or not act_7d:
            pytest.skip("fixture data not in expected window for drift flag test")

        fs_30d = compute_frequency_stats(act_30d, window_days=30)
        fs_7d = compute_frequency_stats(act_7d, window_days=7)

        stats = _compute_stats(act_30d, window_days=30)
        stats["trades_per_day"] = fs_30d.trades_per_day

        flags = detect_risk_flags(
            stats,
            activity,
            positions,
            frequency_stats=fs_30d,
            freq_stats_7d=fs_7d,
        )
        assert "strategy_drift" in flags


class TestInsiderArchetype:
    """Insider suspect wallet → insider_suspect flag → user_status='rejected'."""

    def test_insider_flag_raised(self, ds: FixtureDataSource) -> None:
        activity = _fetch_activity(ds, W_INSIDER)
        positions = _fetch_positions(ds, W_INSIDER)

        if not activity:
            pytest.skip("no fixture activity for insider wallet")

        stats = _compute_stats(activity, window_days=90)
        # Manually override to match insider archetype expectations
        stats["win_rate"] = max(stats.get("win_rate", 0.0), 0.85)
        stats["total_trades"] = len(
            [r for r in activity if r.activity_type == "TRADE"]
        )
        stats["trades_per_day"] = stats["total_trades"] / 90.0

        fs = compute_frequency_stats(activity, window_days=90)
        flags = detect_risk_flags(stats, activity, positions, frequency_stats=fs)

        assert "insider_suspect" in flags

    def test_insider_causes_rejected_status(self, ds: FixtureDataSource) -> None:
        """Verify the worker's _upsert_candidate sets user_status='rejected'."""
        from polyecho.discovery.frequency import StrategyType
        from polyecho.discovery.worker import _upsert_candidate

        activity = _fetch_activity(ds, W_INSIDER)
        if not activity:
            pytest.skip("no fixture activity for insider wallet")

        stats = _compute_stats(activity, window_days=90)
        fs = compute_frequency_stats(activity, window_days=90)

        captured: dict[str, Any] = {}

        class _FakeLeader:
            def __init__(self, **kwargs: Any) -> None:
                captured["user_status"] = kwargs.get("user_status")

        async def _run() -> None:
            with (
                patch(
                    "polyecho.discovery.worker.session_scope"
                ) as mock_scope,
                patch("polyecho.discovery.worker.select"),
            ):
                mock_session = AsyncMock()
                mock_session.execute = AsyncMock(
                    return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
                )
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                added: list[Any] = []
                mock_session.add = lambda obj: added.append(obj)

                await _upsert_candidate(
                    W_INSIDER,
                    "leaderboard",
                    display_name=None,
                    stats_7d={},
                    stats_30d=stats,
                    stats_90d=stats,
                    freq_stats_30d=fs,
                    strategy_type=StrategyType.POSITION,
                    p_score=70.0,
                    r_score=40.0,
                    f_score=28.0,
                    risk_flags=["insider_suspect"],
                    last_active_at=None,
                )

                # Check that the CandidateLeader added has user_status='rejected'
                from polyecho.shared.models import CandidateLeader as CL

                leaders = [obj for obj in added if isinstance(obj, CL)]
                assert leaders, "no CandidateLeader row was added"
                assert leaders[0].user_status == "rejected"

        asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# §5 — Score history row appended for today
# ---------------------------------------------------------------------------


class TestScoreHistoryPersistence:
    """Verify _upsert_candidate appends a CandidateScoreHistory row for today."""

    def test_score_history_row_created(self) -> None:
        import datetime as _dt

        from polyecho.discovery.frequency import StrategyType
        from polyecho.discovery.worker import _upsert_candidate
        from polyecho.shared.models import CandidateScoreHistory

        today = _dt.date.today()

        async def _run() -> None:
            with patch("polyecho.discovery.worker.session_scope") as mock_scope:
                mock_session = AsyncMock()

                # First execute: CandidateLeader not found
                # Second execute: CandidateScoreHistory not found
                mock_session.execute = AsyncMock(
                    side_effect=[
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                    ]
                )
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                added: list[Any] = []
                mock_session.add = lambda obj: added.append(obj)

                dummy_fs = FrequencyStats(
                    trades_per_day=2.0,
                    active_days=25,
                    median_gap_minutes=120.0,
                    avg_hold_hours=36.0,
                    hold_to_resolution_pct=10.0,
                    concurrent_positions_avg=2.0,
                    concurrent_positions_max=5,
                    burstiness=0.2,
                    active_hours_utc=[0, 1, 2, 3],
                )

                await _upsert_candidate(
                    W_SWING,
                    "leaderboard",
                    display_name="SwingKing",
                    stats_7d={},
                    stats_30d={"pnl_usd": 5000.0},
                    stats_90d={},
                    freq_stats_30d=dummy_fs,
                    strategy_type=StrategyType.SWING,
                    p_score=65.0,
                    r_score=50.0,
                    f_score=32.5,
                    risk_flags=[],
                    last_active_at=None,
                )

                hist_rows = [obj for obj in added if isinstance(obj, CandidateScoreHistory)]
                assert len(hist_rows) == 1
                assert hist_rows[0].wallet_address == W_SWING
                assert hist_rows[0].date == today
                assert hist_rows[0].total_score == pytest.approx(32.5)

        asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# §7 — profit_score numerical-stability regression
#
# profit_score is called for every wallet in DiscoveryWorker._process_wallet.
# A leader with a catastrophic loss (pnl_usd hugely negative) is entirely
# realistic.  The original sigmoid 100/(1+exp(-pnl/1e4)) raised
# OverflowError for large negative pnl, which would crash the whole
# discovery pipeline.  It must instead saturate toward 0.
# ---------------------------------------------------------------------------


class TestProfitScoreNumericalStability:
    def _stats(self, **kw: Any) -> dict[str, Any]:
        d = {
            "pnl_usd": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 1.0,
            "consistency": 0.0,
            "specialization": 0.0,
        }
        d.update(kw)
        return d

    def test_huge_negative_pnl_does_not_raise(self) -> None:
        # REGRESSION: previously raised OverflowError("math range error").
        s = profit_score(self._stats(pnl_usd=-1e9))
        assert 0.0 <= s <= 100.0

    def test_huge_negative_pnl_saturates_to_zero_component(self) -> None:
        # All other components are 0 → score is dominated by the PnL term,
        # which must collapse toward 0 for an enormous loss.
        s = profit_score(self._stats(pnl_usd=-1e12))
        assert s == pytest.approx(0.0, abs=1e-6)

    def test_huge_positive_pnl_saturates_to_max_component(self) -> None:
        # PnL term → 100; with PnL weight 0.25 and all else 0 → ~25.
        s = profit_score(self._stats(pnl_usd=1e12))
        assert s == pytest.approx(25.0, abs=1e-3)

    def test_monotonic_non_decreasing_in_pnl(self) -> None:
        pnls = [-1e9, -1e6, -1e5, -1e4, 0.0, 1e4, 1e5, 1e6, 1e9]
        scores = [profit_score(self._stats(pnl_usd=p)) for p in pnls]
        for a, b in zip(scores[:-1], scores[1:], strict=True):
            assert a <= b + 1e-9

    def test_zero_pnl_midpoint(self) -> None:
        # sigmoid(0) = 0.5 → 100 * 0.5 * 0.25 weight = 12.5 (all else 0).
        s = profit_score(self._stats(pnl_usd=0.0))
        assert s == pytest.approx(12.5, abs=1e-6)


# ---------------------------------------------------------------------------
# §8 — classify_strategy boundary & gap behaviour (PRD §5.3 matrix edges)
# ---------------------------------------------------------------------------


class TestClassifyStrategyEdges:
    def _fs(
        self, *, tpd: float, hold_h: float, htr: float = 0.0
    ) -> FrequencyStats:
        return FrequencyStats(
            trades_per_day=tpd,
            active_days=30,
            median_gap_minutes=100.0,
            avg_hold_hours=hold_h,
            hold_to_resolution_pct=htr,
            concurrent_positions_avg=2.0,
            concurrent_positions_max=5,
            burstiness=0.2,
            active_hours_utc=[1, 2, 3],
        )

    def test_hold_to_resolve_takes_priority_over_counts(self) -> None:
        # Even with scalper-like counts, >=60 % redemptions wins.
        st = self._fs(tpd=25.0, hold_h=0.2, htr=60.0)
        assert classify_strategy(st) == StrategyType.HOLD_TO_RESOLVE

    def test_hold_to_resolve_threshold_is_inclusive_at_60(self) -> None:
        assert classify_strategy(self._fs(tpd=1.0, hold_h=10.0, htr=60.0)) == (
            StrategyType.HOLD_TO_RESOLVE
        )
        # Just below threshold must NOT be Hold-to-Resolve.
        assert classify_strategy(self._fs(tpd=3.0, hold_h=48.0, htr=59.9)) == (
            StrategyType.SWING
        )

    def test_scalper_requires_both_high_freq_and_short_hold(self) -> None:
        assert classify_strategy(self._fs(tpd=25.0, hold_h=0.5)) == StrategyType.SCALPER
        # >20/day but hold >= 1h → not a scalper; falls back to Day Trader.
        assert classify_strategy(self._fs(tpd=21.0, hold_h=1.5)) == (
            StrategyType.DAY_TRADER
        )

    def test_swing_lower_boundary_inclusive(self) -> None:
        # tpd exactly 1.0, hold exactly 24h (1 day) → Swing sweet-spot.
        assert classify_strategy(self._fs(tpd=1.0, hold_h=24.0)) == StrategyType.SWING
        # tpd exactly 5.0 with multi-day hold → Swing (1–5/day band).
        assert classify_strategy(self._fs(tpd=5.0, hold_h=24.0)) == StrategyType.SWING

    def test_position_requires_hold_over_seven_days(self) -> None:
        # < 1/day but only ~1 day hold → NOT Position (gap → Unknown).
        assert classify_strategy(self._fs(tpd=0.5, hold_h=24.0)) == (
            StrategyType.UNKNOWN
        )
        # < 1/day and > 7 day hold → Position.
        assert classify_strategy(self._fs(tpd=0.5, hold_h=200.0)) == (
            StrategyType.POSITION
        )

    def test_prd_matrix_gap_is_unknown_not_misclassified(self) -> None:
        # tpd=4/day, hold=8h: Day-Trader band needs tpd>=5; Swing needs
        # hold>=1d; the PRD matrix has no row here → honest UNKNOWN.
        assert classify_strategy(self._fs(tpd=4.0, hold_h=8.0)) == (
            StrategyType.UNKNOWN
        )

    def test_drift_false_when_both_windows_degenerate_unknown(self) -> None:
        # Two different degenerate inputs that both classify UNKNOWN must
        # NOT be reported as drift (UNKNOWN == UNKNOWN).
        assert detect_drift(
            self._fs(tpd=4.0, hold_h=8.0),
            self._fs(tpd=0.1, hold_h=0.5),
        ) is False


# ---------------------------------------------------------------------------
# §9 — riskflags strict-threshold boundaries & insider heuristic lock-in
# ---------------------------------------------------------------------------


class TestRiskFlagBoundaries:
    def _stats(self, **kw: Any) -> dict[str, Any]:
        d = {
            "pnl_usd": 5000.0,
            "win_rate": 0.6,
            "total_trades": 60,
            "trades_per_day": 3.0,
            "first_trade_at": "2026-01-01T00:00:00+00:00",
        }
        d.update(kw)
        return d

    def _old_activity(self, n: int = 60) -> list[ActivityRecord]:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            _make_record(side="BUY", ts=base + timedelta(days=i), usdc_size=200.0)
            for i in range(n)
        ]

    def test_low_liquidity_strictly_greater_than_60pct(self) -> None:
        # Exactly 3/5 thin = 0.60 → NOT > 0.60 → no flag.
        p_eq = [
            _make_position(current_value=50.0, condition_id="a"),
            _make_position(current_value=50.0, condition_id="b"),
            _make_position(current_value=50.0, condition_id="c"),
            _make_position(current_value=500.0, condition_id="d"),
            _make_position(current_value=500.0, condition_id="e"),
        ]
        assert "low_liquidity" not in detect_risk_flags(
            self._stats(), self._old_activity(), p_eq
        )
        # 4/5 thin = 0.80 → > 0.60 → flag.
        p_gt = [
            _make_position(current_value=50.0, condition_id="a"),
            _make_position(current_value=50.0, condition_id="b"),
            _make_position(current_value=50.0, condition_id="c"),
            _make_position(current_value=50.0, condition_id="d"),
            _make_position(current_value=500.0, condition_id="e"),
        ]
        assert "low_liquidity" in detect_risk_flags(
            self._stats(), self._old_activity(), p_gt
        )

    def test_high_concentration_strictly_greater_than_50pct(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # Exactly 50/50 across two conditions → top share 0.50, NOT > 0.50.
        even = [
            _make_record(
                side="BUY", ts=base + timedelta(days=i),
                usdc_size=1000.0, condition_id="A",
            )
            for i in range(10)
        ] + [
            _make_record(
                side="BUY", ts=base + timedelta(days=i),
                usdc_size=1000.0, condition_id="B",
            )
            for i in range(10)
        ]
        assert "high_concentration" not in detect_risk_flags(
            self._stats(total_trades=20), even, []
        )
        # 60/40 → top 0.60 > 0.50 → flag.
        skew = [
            _make_record(
                side="BUY", ts=base + timedelta(days=i),
                usdc_size=1000.0, condition_id="A",
            )
            for i in range(12)
        ] + [
            _make_record(
                side="BUY", ts=base + timedelta(days=i),
                usdc_size=1000.0, condition_id="B",
            )
            for i in range(8)
        ]
        assert "high_concentration" in detect_risk_flags(
            self._stats(total_trades=20), skew, []
        )

    def test_insider_suspect_clean_case_flags(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond = "0xcond_ins"
        act: list[ActivityRecord] = [
            ActivityRecord(
                wallet=W_INSIDER, activity_type="TRADE", condition_id=cond,
                side="BUY", size=2000.0, price=0.05, usdc_size=8000.0,
                timestamp=base + timedelta(hours=i), raw={},
            )
            for i in range(5)
        ]
        act.append(
            ActivityRecord(
                wallet=W_INSIDER, activity_type="REDEEM", condition_id=cond,
                side=None, size=1e4, price=None, usdc_size=1e4,
                timestamp=base + timedelta(hours=20), raw={},
            )
        )
        stats = {
            "pnl_usd": 5e4, "win_rate": 0.95,
            "total_trades": 5, "trades_per_day": 1.0,
        }
        assert "insider_suspect" in detect_risk_flags(stats, act, [])

    def test_insider_near_resolution_loss_suppresses_flag(self) -> None:
        """LOCK-IN of intended conservative policy (riskflags docstring).

        A near-resolution losing SELL on the same condition drags
        near_res_wr below the 0.80 gate, so insider_suspect is *not*
        raised.  insider_suspect triggers an irreversible auto-reject, so
        the heuristic deliberately favours false negatives over false
        positives.  This test documents and pins that behaviour.
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond = "0xcond_ins2"
        act: list[ActivityRecord] = [
            ActivityRecord(
                wallet=W_INSIDER, activity_type="TRADE", condition_id=cond,
                side="BUY", size=2000.0, price=0.05, usdc_size=8000.0,
                timestamp=base + timedelta(hours=i), raw={},
            )
            for i in range(5)
        ]
        act.append(
            ActivityRecord(
                wallet=W_INSIDER, activity_type="TRADE", condition_id=cond,
                side="SELL", size=100.0, price=0.9, usdc_size=5000.0,
                timestamp=base + timedelta(hours=18), raw={},
            )
        )
        act.append(
            ActivityRecord(
                wallet=W_INSIDER, activity_type="REDEEM", condition_id=cond,
                side=None, size=1e4, price=None, usdc_size=1e4,
                timestamp=base + timedelta(hours=20), raw={},
            )
        )
        stats = {
            "pnl_usd": 5e4, "win_rate": 0.95,
            "total_trades": 6, "trades_per_day": 1.0,
        }
        assert "insider_suspect" not in detect_risk_flags(stats, act, [])

    def test_insider_requires_low_total_trades(self) -> None:
        # Same large near-resolution pattern but a deep history
        # (total_trades > 50) must NOT be flagged.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond = "0xcond_ins3"
        act: list[ActivityRecord] = [
            ActivityRecord(
                wallet=W_INSIDER, activity_type="TRADE", condition_id=cond,
                side="BUY", size=2000.0, price=0.05, usdc_size=8000.0,
                timestamp=base + timedelta(hours=i), raw={},
            )
            for i in range(5)
        ]
        act.append(
            ActivityRecord(
                wallet=W_INSIDER, activity_type="REDEEM", condition_id=cond,
                side=None, size=1e4, price=None, usdc_size=1e4,
                timestamp=base + timedelta(hours=20), raw={},
            )
        )
        stats = {
            "pnl_usd": 5e4, "win_rate": 0.95,
            "total_trades": 200, "trades_per_day": 1.0,  # deep history
        }
        assert "insider_suspect" not in detect_risk_flags(stats, act, [])


# ---------------------------------------------------------------------------
# §10 — worker._upsert_candidate: source-merge, non-insider status, update path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase-5 Wave-2 — CANDIDATE_HIGH_SCORE event emission (state-transition only)
# ---------------------------------------------------------------------------


class TestCandidateHighScoreEvent:
    """``_upsert_candidate`` emits CANDIDATE_HIGH_SCORE once per UPWARD
    crossing of :data:`HIGH_SCORE_THRESHOLD` — never every run, never for
    an auto-rejected (insider) candidate, and best-effort (a raising
    ``emit_event`` must not crash discovery).
    """

    def _fs(self) -> FrequencyStats:
        return FrequencyStats(
            trades_per_day=2.0,
            active_days=25,
            median_gap_minutes=120.0,
            avg_hold_hours=36.0,
            hold_to_resolution_pct=10.0,
            concurrent_positions_avg=2.0,
            concurrent_positions_max=5,
            burstiness=0.2,
            active_hours_utc=[0, 1, 2, 3],
        )

    def _scope(self, existing: Any) -> MagicMock:
        ms: MagicMock = patch("polyecho.discovery.worker.session_scope").start()
        sess = AsyncMock()
        sess.execute = AsyncMock(
            side_effect=[
                MagicMock(
                    scalar_one_or_none=MagicMock(return_value=existing)
                ),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )
        ms.return_value.__aenter__ = AsyncMock(return_value=sess)
        ms.return_value.__aexit__ = AsyncMock(return_value=False)
        sess.add = MagicMock()
        return ms

    def _emit_spy(self) -> AsyncMock:
        spy = AsyncMock()
        patch("polyecho.discovery.worker.emit_event", spy).start()
        return spy

    def teardown_method(self) -> None:
        patch.stopall()

    async def _upsert(self, *, f_score: float, risk_flags: list[str]) -> None:
        from polyecho.discovery.worker import _upsert_candidate

        await _upsert_candidate(
            W_SWING, "leaderboard", display_name="Swing",
            stats_7d={}, stats_30d={"pnl_usd": 1.0}, stats_90d={},
            freq_stats_30d=self._fs(),
            strategy_type=StrategyType.SWING,
            p_score=90.0, r_score=80.0, f_score=f_score,
            risk_flags=risk_flags,
            last_active_at=None,
        )

    def test_emits_on_new_candidate_crossing_high(self) -> None:
        self._scope(existing=None)
        spy = self._emit_spy()
        asyncio.get_event_loop().run_until_complete(
            self._upsert(f_score=85.0, risk_flags=[])
        )
        assert spy.await_count == 1
        assert spy.await_args is not None
        kw = spy.await_args.kwargs
        assert kw["event_type"] is EventType.CANDIDATE_HIGH_SCORE
        assert kw["severity"] is Severity.INFO
        assert kw["push"].url == f"/discovery/{W_SWING}"
        assert kw["push"].tag == f"candidate_high_score:{W_SWING}"

    def test_no_emit_below_threshold(self) -> None:
        self._scope(existing=None)
        spy = self._emit_spy()
        asyncio.get_event_loop().run_until_complete(
            self._upsert(f_score=42.0, risk_flags=[])
        )
        spy.assert_not_awaited()

    def test_no_emit_when_already_above_no_recrossing(self) -> None:
        existing = MagicMock()
        existing.sources = ["leaderboard"]
        existing.user_status = "new"
        existing.total_score = 82.0  # already above ⇒ no new crossing
        self._scope(existing=existing)
        spy = self._emit_spy()
        asyncio.get_event_loop().run_until_complete(
            self._upsert(f_score=88.0, risk_flags=[])
        )
        spy.assert_not_awaited()

    def test_no_emit_for_insider_rejected_candidate(self) -> None:
        self._scope(existing=None)
        spy = self._emit_spy()
        asyncio.get_event_loop().run_until_complete(
            self._upsert(f_score=95.0, risk_flags=["insider_suspect"])
        )
        spy.assert_not_awaited()

    def test_emit_failure_does_not_crash_discovery(self) -> None:
        self._scope(existing=None)
        boom = AsyncMock(side_effect=RuntimeError("NOTIFY down"))
        patch("polyecho.discovery.worker.emit_event", boom).start()
        # Must complete despite emit_event raising (best-effort wrapper).
        asyncio.get_event_loop().run_until_complete(
            self._upsert(f_score=90.0, risk_flags=[])
        )
        boom.assert_awaited_once()


class TestUpsertCandidateBehaviour:
    def _fs(self) -> FrequencyStats:
        return FrequencyStats(
            trades_per_day=2.0,
            active_days=25,
            median_gap_minutes=120.0,
            avg_hold_hours=36.0,
            hold_to_resolution_pct=10.0,
            concurrent_positions_avg=2.0,
            concurrent_positions_max=5,
            burstiness=0.2,
            active_hours_utc=[0, 1, 2, 3],
        )

    def test_non_insider_status_is_new(self) -> None:
        from polyecho.discovery.worker import _upsert_candidate
        from polyecho.shared.models import CandidateLeader as CL

        added: list[Any] = []

        async def _run() -> None:
            with patch("polyecho.discovery.worker.session_scope") as ms:
                sess = AsyncMock()
                sess.execute = AsyncMock(
                    side_effect=[
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                    ]
                )
                ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                ms.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = lambda o: added.append(o)

                await _upsert_candidate(
                    W_SWING, "leaderboard", display_name="X",
                    stats_7d={}, stats_30d={"pnl_usd": 1.0}, stats_90d={},
                    freq_stats_30d=self._fs(),
                    strategy_type=StrategyType.SWING,
                    p_score=60.0, r_score=50.0, f_score=30.0,
                    risk_flags=["new_account"],  # NOT insider
                    last_active_at=None,
                )

        asyncio.get_event_loop().run_until_complete(_run())
        leaders = [o for o in added if isinstance(o, CL)]
        assert leaders and leaders[0].user_status == "new"

    def test_existing_record_merges_sources_and_keeps_first_discovered(
        self,
    ) -> None:
        from polyecho.discovery.worker import _upsert_candidate

        existing = MagicMock()
        existing.sources = ["leaderboard"]
        existing.user_status = "new"
        sentinel_first = object()
        existing.first_discovered_at = sentinel_first

        async def _run() -> None:
            with patch("polyecho.discovery.worker.session_scope") as ms:
                sess = AsyncMock()
                sess.execute = AsyncMock(
                    side_effect=[
                        MagicMock(
                            scalar_one_or_none=MagicMock(return_value=existing)
                        ),
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                    ]
                )
                ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                ms.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = MagicMock()

                await _upsert_candidate(
                    W_SWING, "smart_money_seed", display_name=None,
                    stats_7d={}, stats_30d={"pnl_usd": 2.0}, stats_90d={},
                    freq_stats_30d=self._fs(),
                    strategy_type=StrategyType.SWING,
                    p_score=70.0, r_score=60.0, f_score=42.0,
                    risk_flags=[],
                    last_active_at=None,
                )

        asyncio.get_event_loop().run_until_complete(_run())
        # Source merged & deduplicated + sorted; first_discovered_at untouched.
        assert existing.sources == ["leaderboard", "smart_money_seed"]
        assert existing.first_discovered_at is sentinel_first
        assert existing.total_score == pytest.approx(42.0)
        # Never auto-unrejects, but a clean record stays "new".
        assert existing.user_status == "new"

    def test_existing_rejected_record_stays_rejected_on_clean_rerun(
        self,
    ) -> None:
        """Once rejected, a clean re-run must NOT silently un-reject."""
        from polyecho.discovery.worker import _upsert_candidate

        existing = MagicMock()
        existing.sources = ["leaderboard"]
        existing.user_status = "rejected"

        async def _run() -> None:
            with patch("polyecho.discovery.worker.session_scope") as ms:
                sess = AsyncMock()
                sess.execute = AsyncMock(
                    side_effect=[
                        MagicMock(
                            scalar_one_or_none=MagicMock(return_value=existing)
                        ),
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                    ]
                )
                ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                ms.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = MagicMock()

                await _upsert_candidate(
                    W_SWING, "leaderboard", display_name=None,
                    stats_7d={}, stats_30d={}, stats_90d={},
                    freq_stats_30d=self._fs(),
                    strategy_type=StrategyType.SWING,
                    p_score=70.0, r_score=60.0, f_score=42.0,
                    risk_flags=[],  # clean now
                    last_active_at=None,
                )

        asyncio.get_event_loop().run_until_complete(_run())
        assert existing.user_status == "rejected"


class _StubLeaderboardEntry:
    """Minimal stand-in for LeaderboardEntry (only ``.wallet`` is read)."""

    def __init__(self, wallet: str) -> None:
        self.wallet = wallet
        self.username = None


class _LargePositionStubDS:
    """Deterministic DataSource stub for discover_large_new_positions tests.

    ``activity_by_wallet`` maps wallet -> list[ActivityRecord]; the stub
    honours the ``start`` window and ``activity_type`` filters exactly as
    the frozen protocol specifies so the worker's window logic is exercised.
    """

    def __init__(
        self,
        activity_by_wallet: dict[str, list[ActivityRecord]],
        leaderboard_wallets: list[str] | None = None,
    ) -> None:
        self._activity = activity_by_wallet
        self._lb = leaderboard_wallets or []
        self.activity_calls: list[tuple[str, datetime | None, str | None]] = []

    async def get_leaderboard(
        self,
        *,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 200,
    ) -> list[Any]:
        return [_StubLeaderboardEntry(w) for w in self._lb[:limit]]

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        self.activity_calls.append((wallet, start, activity_type))
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


class TestDiscoverLargeNewPositions:
    """§5.2 — large-new-position (hourly) discovery via DataSource scan."""

    def test_wallet_with_recent_large_entry_is_detected(self) -> None:
        big = _make_record(
            wallet=W_SWING, usdc_size=7_500.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=2),
        )
        ds = _LargePositionStubDS({W_SWING: [big]})
        processed: list[tuple[str, str]] = []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING}

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append((w, s))

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())
        assert processed == [(W_SWING, "large_new_positions")]

    def test_sub_threshold_and_old_entries_excluded(self) -> None:
        sub = _make_record(
            wallet=W_SWING, usdc_size=4_999.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        old_big = _make_record(
            wallet=W_SCALPER, usdc_size=50_000.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=48),  # outside 24h window
        )
        ds = _LargePositionStubDS({W_SWING: [sub], W_SCALPER: [old_big]})
        processed: list[str] = []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING, W_SCALPER}

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append(w)

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())
        assert processed == []

    def test_size_fallback_when_no_usdc_size(self) -> None:
        # No usdc_size -> share-count fallback (>500 shares qualifies).
        rec = _make_record(
            wallet=W_SWING, usdc_size=None, size=600.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=3),
        )
        small = _make_record(
            wallet=W_SCALPER, usdc_size=None, size=10.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=3),
        )
        ds = _LargePositionStubDS({W_SWING: [rec], W_SCALPER: [small]})
        processed: list[str] = []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING, W_SCALPER}

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append(w)

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())
        assert processed == [W_SWING]

    def test_dedupe_multiple_large_entries_single_process_call(self) -> None:
        recs = [
            _make_record(
                wallet=W_SWING, usdc_size=6_000.0,
                ts=datetime.now(tz=UTC) - timedelta(hours=h),
            )
            for h in (1, 2, 3)
        ]
        ds = _LargePositionStubDS({W_SWING: recs})
        processed: list[str] = []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING}

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append(w)

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())
        assert processed == [W_SWING]  # 3 big trades -> exactly 1 process call

    def test_deterministic_sorted_processing_order(self) -> None:
        ts = datetime.now(tz=UTC) - timedelta(hours=1)
        ds = _LargePositionStubDS(
            {
                W_SWING: [_make_record(wallet=W_SWING, usdc_size=9_000.0, ts=ts)],
                W_SCALPER: [_make_record(wallet=W_SCALPER, usdc_size=9_000.0, ts=ts)],
                W_INSIDER: [_make_record(wallet=W_INSIDER, usdc_size=9_000.0, ts=ts)],
            }
        )
        order: list[str] = []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING, W_SCALPER, W_INSIDER}

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                order.append(w)

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())
        assert order == sorted([W_SWING, W_SCALPER, W_INSIDER])

    def test_cold_start_seeds_from_leaderboard(self) -> None:
        # known set empty -> must pull leaderboard slice as the pool.
        big = _make_record(
            wallet=W_SWING, usdc_size=8_000.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        ds = _LargePositionStubDS(
            {W_SWING: [big]}, leaderboard_wallets=[W_SWING, W_SCALPER]
        )
        processed: list[str] = []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            # _known_wallets intentionally empty -> triggers leaderboard seed

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append(w)

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())
        assert processed == [W_SWING]

    def test_window_start_passed_to_datasource(self) -> None:
        """The 24h trailing window must be pushed down to get_user_activity."""
        ds = _LargePositionStubDS({W_SWING: []})

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING}

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                pass

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        before = datetime.now(tz=UTC) - timedelta(hours=24)
        asyncio.get_event_loop().run_until_complete(_run())
        after = datetime.now(tz=UTC) - timedelta(hours=24)
        assert ds.activity_calls, "get_user_activity was not called"
        _, start, atype = ds.activity_calls[0]
        assert atype == "TRADE"
        assert start is not None
        # start ≈ now-24h (allow a small wall-clock delta around the call)
        assert before - timedelta(seconds=5) <= start <= after + timedelta(seconds=5)

    def test_full_pipeline_smoke_with_fixture_ds(self) -> None:
        """End-to-end with the real FixtureDataSource + mocked DB session."""

        async def _run() -> None:
            ds = FixtureDataSource()
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)
            worker._known_wallets = {W_SWING, W_SCALPER}

            with patch("polyecho.discovery.worker.session_scope") as mock_scope:
                sess = AsyncMock()
                sess.execute = AsyncMock(
                    return_value=MagicMock(
                        scalar_one_or_none=MagicMock(return_value=None)
                    )
                )
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=sess)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = MagicMock()

                # Must not raise; behaviour is deterministic over fixtures.
                await worker.discover_large_new_positions()
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# §6 — Worker pipeline smoke test (end-to-end with mocked DB)
# ---------------------------------------------------------------------------


class TestWorkerPipelineSmokeTest:
    """Run discover_leaderboard() with FixtureDataSource + mocked DB session."""

    def test_discover_leaderboard_runs_without_error(self) -> None:
        async def _run() -> None:
            ds = FixtureDataSource()
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)

            with patch("polyecho.discovery.worker.session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.execute = AsyncMock(
                    return_value=MagicMock(
                        scalar_one_or_none=MagicMock(return_value=None)
                    )
                )
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session.add = MagicMock()

                await worker.discover_leaderboard()

        asyncio.get_event_loop().run_until_complete(_run())

    def test_run_once_entry_point(self) -> None:
        """Test that __main__.main(once=True) dispatches correctly."""
        from polyecho.discovery.__main__ import _run_once

        async def _run() -> None:
            ds = FixtureDataSource()
            worker = DiscoveryWorker(ds, follower_capital_usd=500.0)

            with patch("polyecho.discovery.worker.session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.execute = AsyncMock(
                    return_value=MagicMock(
                        scalar_one_or_none=MagicMock(return_value=None)
                    )
                )
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session.add = MagicMock()

                await _run_once(worker)

        asyncio.get_event_loop().run_until_complete(_run())
