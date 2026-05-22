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
import contextlib
import sys
import types
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
from polyecho.discovery.scoring import (
    MIN_SAMPLE_TRADES,
    SCORE_VERSION,
    final_score,
    profit_score,
    replicability_score,
    score_metadata,
)
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
            records.append(_make_record(ts=base + offset + timedelta(minutes=i), condition_id="b"))
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
        fs30 = self._fs(tpd=3.0, hold_h=48.0)  # SWING
        fs7 = self._fs(tpd=2.0, hold_h=36.0)  # SWING
        assert detect_drift(fs30, fs7) is False

    def test_drift_detected(self) -> None:
        fs30 = self._fs(tpd=3.0, hold_h=48.0)  # SWING
        fs7 = self._fs(tpd=25.0, hold_h=0.3)  # SCALPER
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


class TestScoringCollapseRegression:
    """Recurrence guard for the all-zeros collapse (burstiness clip bug).

    Pre-fix ``frequency.compute_frequency_stats`` mapped the
    inter-trade-gap CV with ``clip(cv, 0, 1)``.  Real gap distributions
    are heavy-tailed so ``cv >= 1`` for essentially every leader,
    pinning ``burstiness`` at EXACTLY 1.0; ``replicability_score`` then
    computed ``burstiness_factor = clip(1 - 1.0, 0, 1) = 0`` inside a
    four-factor PRODUCT, zeroing replicability AND the multiplicative
    ``final_score`` for ~every candidate (dashboard showed all 0).

    The existing scoring unit tests hand-fed ``burstiness=0.2`` and
    never exercised the real frequency→scoring path, so the collapse
    was invisible to them.  These bind the two layers and assert a
    realistic leader yields STRICTLY non-zero scores: they FAIL on the
    old ``clip(cv,0,1)`` impl, pass on the saturating ``cv/(1+cv)``.
    """

    @staticmethod
    def _realistic_irregular_activity() -> list[ActivityRecord]:
        # Real-world-ish leader: irregular (heavy-tailed) inter-trade
        # gaps whose CV is > 1 — exactly the regime the old clip pinned
        # to burstiness == 1.0.
        base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        gaps_min = [45, 90, 30, 240, 60, 150, 35, 600]  # CV > 1
        records: list[ActivityRecord] = []
        t = base
        for i in range(48):
            records.append(_make_record(ts=t, condition_id=f"0xc{i % 7}"))
            t = t + timedelta(minutes=gaps_min[i % len(gaps_min)])
        return records

    def test_burstiness_never_pins_at_one_for_heavy_tailed_gaps(
        self,
    ) -> None:
        stats = compute_frequency_stats(self._realistic_irregular_activity(), window_days=30)
        # The invariant the clip bug violated: a finite-CV leader must
        # NOT saturate burstiness to exactly 1.0 (which would hard-zero
        # the whole multiplicative replicability score).
        assert 0.0 < stats.burstiness < 1.0

    def test_realistic_leader_has_nonzero_replicability_and_total(
        self,
    ) -> None:
        fs = compute_frequency_stats(self._realistic_irregular_activity(), window_days=30).to_dict()

        rep = replicability_score(fs, follower_capital_usd=1000.0)
        assert rep > 0.0, "replicability collapsed to 0 — the burstiness clip regression is back"

        prof = profit_score(
            {
                "pnl_usd": 80_000.0,
                "win_rate": 0.62,
                "sharpe": 1.5,
                "max_drawdown": 0.3,
                "consistency": 0.6,
                "specialization": 0.5,
            }
        )
        fin = final_score(prof, rep)
        assert prof > 0.0
        assert fin > 0.0, (
            "final_score collapsed to 0 for a realistic profitable, "
            "copyable leader — discovery ranking is all-zeros again"
        )

    def test_proportional(self) -> None:
        assert final_score(80.0, 50.0) == pytest.approx(40.0)

    def test_output_in_range(self) -> None:
        assert 0.0 <= final_score(60.0, 70.0) <= 100.0


# ---------------------------------------------------------------------------
# §2b — score_metadata: version + low-confidence + unavailable-vs-zero
#       (PolyHermes deep-dive B2 — copy-trading-risk-seatbelt)
# ---------------------------------------------------------------------------


class TestScoreMetadata:
    """B2 — score-trust metadata: version string + low-confidence flag.

    A positive score on a tiny sample must NOT be read as "verified
    profitable", and a failed/missing data fetch must NOT be counted as a
    confirmed zero.  ``score_metadata`` records both signals inside the
    existing ``stats`` JSONB payload (no new DB column).
    """

    def test_score_version_recorded(self) -> None:
        # A large, fully-measured sample is still stamped with the version
        # so a future formula change is distinguishable in the data.
        meta = score_metadata({"total_trades": 500, "unavailable_inputs": []})
        assert meta["score_version"] == SCORE_VERSION
        assert isinstance(SCORE_VERSION, str) and SCORE_VERSION

    def test_large_fully_measured_sample_is_trustworthy(self) -> None:
        meta = score_metadata({"total_trades": 500, "unavailable_inputs": []})
        assert meta["low_confidence"] is False
        assert meta["low_confidence_reasons"] == []
        assert meta["sample_trades"] == 500

    def test_small_sample_flagged_low_confidence(self) -> None:
        # Below the trade floor → low-confidence even with all inputs measured.
        meta = score_metadata({"total_trades": MIN_SAMPLE_TRADES - 1, "unavailable_inputs": []})
        assert meta["low_confidence"] is True
        assert "small_sample" in meta["low_confidence_reasons"]

    def test_sample_exactly_at_floor_is_trustworthy(self) -> None:
        meta = score_metadata({"total_trades": MIN_SAMPLE_TRADES, "unavailable_inputs": []})
        assert "small_sample" not in meta["low_confidence_reasons"]

    def test_unavailable_input_flagged_low_confidence(self) -> None:
        # A genuinely unavailable input (failed/missing fetch) on an
        # otherwise large sample must STILL flag low-confidence — it is not
        # a confirmed zero.
        meta = score_metadata({"total_trades": 500, "unavailable_inputs": ["sharpe"]})
        assert meta["low_confidence"] is True
        assert "unavailable_inputs" in meta["low_confidence_reasons"]

    def test_missing_audit_key_defaults_safe(self) -> None:
        # An absent ``unavailable_inputs`` key means "fully measured" — it
        # must not crash and must not spuriously flag.
        meta = score_metadata({"total_trades": 500})
        assert meta["low_confidence"] is False


class TestComputeStatsUnavailableVsZero:
    """B2 — ``_compute_stats`` distinguishes 'no data' from a measured 0."""

    def test_no_trades_marks_every_input_unavailable(self) -> None:
        stats = _compute_stats([], window_days=30)
        # The numeric placeholders are present (so profit_score won't
        # crash) but every input is audited as unavailable, NOT a real 0.
        assert set(stats["unavailable_inputs"]) == {
            "pnl_usd",
            "win_rate",
            "sharpe",
            "max_drawdown",
            "consistency",
            "specialization",
        }
        # And score_metadata turns that into a low-confidence flag.
        assert score_metadata(stats)["low_confidence"] is True

    def test_pure_buy_sample_marks_win_rate_unavailable(self) -> None:
        # All BUYs, no SELL → there is no win/lose ratio to measure.
        # win_rate must NOT be read as a confirmed 0 % (a losing leader).
        base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        buys = [_make_record(side="BUY", ts=base + timedelta(hours=i)) for i in range(12)]
        stats = _compute_stats(buys, window_days=30)
        assert "win_rate" in stats["unavailable_inputs"]

    def test_single_day_sample_marks_sharpe_unavailable(self) -> None:
        # < 2 daily-PnL points → no deviation → Sharpe cannot be measured.
        base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        same_day = [
            _make_record(side="SELL", ts=base + timedelta(minutes=10 * i)) for i in range(12)
        ]
        stats = _compute_stats(same_day, window_days=30)
        assert "sharpe" in stats["unavailable_inputs"]

    def test_rich_multiday_sample_has_no_unavailable_inputs(self) -> None:
        # Priced SELLs spanning multiple days → win_rate AND sharpe are
        # both genuinely measurable; the audit list is empty.
        base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        records = [
            _make_record(side="SELL", usdc_size=20.0, ts=base + timedelta(days=i))
            for i in range(15)
        ]
        stats = _compute_stats(records, window_days=30)
        assert stats["unavailable_inputs"] == []
        assert score_metadata(stats)["low_confidence"] is False


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
        fs_30d = self._freq_stats(tpd=3.0, hold_h=48.0)  # SWING
        fs_7d = self._freq_stats(tpd=25.0, hold_h=0.3)  # SCALPER
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
            "win_rate": 0.95,  # > 0.80
            "total_trades": 5,  # <= 50
            "trades_per_day": 1.0,  # < 5
        }
        flags = detect_risk_flags(stats, activity, [])
        assert "insider_suspect" in flags

    def test_low_liquidity_flag(self) -> None:
        positions = [
            _make_position(current_value=50.0),  # thin
            _make_position(current_value=30.0, condition_id="0xb"),  # thin
            _make_position(current_value=20.0, condition_id="0xc"),  # thin
        ]
        base = datetime(2026, 1, 1, tzinfo=UTC)
        activity = [_make_record(side="BUY", ts=base + timedelta(days=i)) for i in range(40)]
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
        stats["total_trades"] = len([r for r in activity if r.activity_type == "TRADE"])
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
                patch("polyecho.discovery.worker.session_scope") as mock_scope,
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

        # R10 I5: production keys history by ``datetime.now(tz=UTC).date()``
        # (see worker._upsert_candidate ``today``).  The OLD test used the
        # process-LOCAL ``date.today()`` which differs from the UTC date in
        # a non-UTC CI runner near midnight → spurious failure.  Match
        # production exactly: use the UTC calendar date.
        today = _dt.datetime.now(tz=_dt.UTC).date()

        executed: list[Any] = []

        async def _run() -> None:
            with patch("polyecho.discovery.worker.session_scope") as mock_scope:
                mock_session = AsyncMock()

                # R10 C1: the history row is now written via an atomic
                # ``INSERT … ON CONFLICT (wallet_address, date) DO UPDATE``
                # (``session.execute``), NOT ``session.add`` of an ORM
                # object.  execute #1 = candidate SELECT … FOR UPDATE
                # (no existing row); execute #2 = the history ON CONFLICT
                # statement we capture & assert on.
                async def _exec(stmt: Any) -> MagicMock:
                    executed.append(stmt)
                    return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

                mock_session.execute = AsyncMock(side_effect=_exec)
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

        asyncio.get_event_loop().run_until_complete(_run())

        # No ORM CandidateScoreHistory object is added any more (R10 C1).
        # Exactly two statements executed: candidate SELECT … FOR UPDATE,
        # then the history INSERT … ON CONFLICT … DO UPDATE.
        assert len(executed) == 2
        hist_stmt = executed[1]
        compiled = hist_stmt.compile()
        upper = str(compiled).upper()
        assert "INSERT INTO CANDIDATE_SCORE_HISTORY" in upper
        assert "ON CONFLICT" in upper
        assert "DO UPDATE" in upper
        # The conflict target is the (wallet_address, date) composite PK.
        assert "WALLET_ADDRESS" in upper and "DATE" in upper
        # Bound parameters carry the exact values production keyed by.
        params = compiled.params
        assert params["wallet_address"] == W_SWING
        assert params["date"] == today
        assert params["total_score"] == pytest.approx(32.5)


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
    def _fs(self, *, tpd: float, hold_h: float, htr: float = 0.0) -> FrequencyStats:
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
        assert classify_strategy(self._fs(tpd=3.0, hold_h=48.0, htr=59.9)) == (StrategyType.SWING)

    def test_scalper_requires_both_high_freq_and_short_hold(self) -> None:
        assert classify_strategy(self._fs(tpd=25.0, hold_h=0.5)) == StrategyType.SCALPER
        # >20/day but hold >= 1h → not a scalper; falls back to Day Trader.
        assert classify_strategy(self._fs(tpd=21.0, hold_h=1.5)) == (StrategyType.DAY_TRADER)

    def test_swing_lower_boundary_inclusive(self) -> None:
        # tpd exactly 1.0, hold exactly 24h (1 day) → Swing sweet-spot.
        assert classify_strategy(self._fs(tpd=1.0, hold_h=24.0)) == StrategyType.SWING
        # tpd exactly 5.0 with multi-day hold → Swing (1–5/day band).
        assert classify_strategy(self._fs(tpd=5.0, hold_h=24.0)) == StrategyType.SWING

    def test_position_requires_hold_over_seven_days(self) -> None:
        # < 1/day but only ~1 day hold → NOT Position (gap → Unknown).
        assert classify_strategy(self._fs(tpd=0.5, hold_h=24.0)) == (StrategyType.UNKNOWN)
        # < 1/day and > 7 day hold → Position.
        assert classify_strategy(self._fs(tpd=0.5, hold_h=200.0)) == (StrategyType.POSITION)

    def test_prd_matrix_gap_is_unknown_not_misclassified(self) -> None:
        # tpd=4/day, hold=8h: Day-Trader band needs tpd>=5; Swing needs
        # hold>=1d; the PRD matrix has no row here → honest UNKNOWN.
        assert classify_strategy(self._fs(tpd=4.0, hold_h=8.0)) == (StrategyType.UNKNOWN)

    def test_drift_false_when_both_windows_degenerate_unknown(self) -> None:
        # Two different degenerate inputs that both classify UNKNOWN must
        # NOT be reported as drift (UNKNOWN == UNKNOWN).
        assert (
            detect_drift(
                self._fs(tpd=4.0, hold_h=8.0),
                self._fs(tpd=0.1, hold_h=0.5),
            )
            is False
        )


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
            _make_record(side="BUY", ts=base + timedelta(days=i), usdc_size=200.0) for i in range(n)
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
        assert "low_liquidity" not in detect_risk_flags(self._stats(), self._old_activity(), p_eq)
        # 4/5 thin = 0.80 → > 0.60 → flag.
        p_gt = [
            _make_position(current_value=50.0, condition_id="a"),
            _make_position(current_value=50.0, condition_id="b"),
            _make_position(current_value=50.0, condition_id="c"),
            _make_position(current_value=50.0, condition_id="d"),
            _make_position(current_value=500.0, condition_id="e"),
        ]
        assert "low_liquidity" in detect_risk_flags(self._stats(), self._old_activity(), p_gt)

    def test_high_concentration_strictly_greater_than_50pct(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # Exactly 50/50 across two conditions → top share 0.50, NOT > 0.50.
        even = [
            _make_record(
                side="BUY",
                ts=base + timedelta(days=i),
                usdc_size=1000.0,
                condition_id="A",
            )
            for i in range(10)
        ] + [
            _make_record(
                side="BUY",
                ts=base + timedelta(days=i),
                usdc_size=1000.0,
                condition_id="B",
            )
            for i in range(10)
        ]
        assert "high_concentration" not in detect_risk_flags(self._stats(total_trades=20), even, [])
        # 60/40 → top 0.60 > 0.50 → flag.
        skew = [
            _make_record(
                side="BUY",
                ts=base + timedelta(days=i),
                usdc_size=1000.0,
                condition_id="A",
            )
            for i in range(12)
        ] + [
            _make_record(
                side="BUY",
                ts=base + timedelta(days=i),
                usdc_size=1000.0,
                condition_id="B",
            )
            for i in range(8)
        ]
        assert "high_concentration" in detect_risk_flags(self._stats(total_trades=20), skew, [])

    def test_insider_suspect_clean_case_flags(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond = "0xcond_ins"
        act: list[ActivityRecord] = [
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="TRADE",
                condition_id=cond,
                side="BUY",
                size=2000.0,
                price=0.05,
                usdc_size=8000.0,
                timestamp=base + timedelta(hours=i),
                raw={},
            )
            for i in range(5)
        ]
        act.append(
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="REDEEM",
                condition_id=cond,
                side=None,
                size=1e4,
                price=None,
                usdc_size=1e4,
                timestamp=base + timedelta(hours=20),
                raw={},
            )
        )
        stats = {
            "pnl_usd": 5e4,
            "win_rate": 0.95,
            "total_trades": 5,
            "trades_per_day": 1.0,
        }
        assert "insider_suspect" in detect_risk_flags(stats, act, [])

    def test_insider_near_resolution_loss_suppresses_flag(self) -> None:
        """LOCK-IN of the conservative policy under R10 I3 SYMMETRIC
        per-condition near-resolution counting.

        R10 I3: ``near_res_wins`` and ``near_res_total`` are now both
        counted per-CONDITION (the old code added wins per-condition but
        total per-SELL-trade — a meaningless mixed-granularity ratio).
        Under the corrected symmetric rule, a near-resolution losing SELL
        on its OWN condition (no near-res BUY there) counts as a separate
        non-win condition and drags ``near_res_wr`` below the 0.80 gate,
        so insider_suspect is *not* raised.  insider_suspect triggers an
        irreversible auto-reject, so the heuristic still deliberately
        favours false negatives; this pins that behaviour under the
        fixed, consistent denominator.
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond_win = "0xcond_ins2_win"
        cond_loss = "0xcond_ins2_loss"
        # Condition A: 5 near-resolution BUYs then a REDEEM → a "win"
        # condition (held into resolution, redeemed).
        act: list[ActivityRecord] = [
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="TRADE",
                condition_id=cond_win,
                side="BUY",
                size=2000.0,
                price=0.05,
                usdc_size=8000.0,
                timestamp=base + timedelta(hours=i),
                raw={},
            )
            for i in range(5)
        ]
        act.append(
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="REDEEM",
                condition_id=cond_win,
                side=None,
                size=1e4,
                price=None,
                usdc_size=1e4,
                timestamp=base + timedelta(hours=20),
                raw={},
            )
        )
        # Condition B: only a near-resolution SELL (no BUY) before its
        # own resolution → counts toward the total but NOT as a win, so
        # near_res_wr = 1/2 = 0.5 < 0.80.
        act.append(
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="TRADE",
                condition_id=cond_loss,
                side="SELL",
                size=100.0,
                price=0.9,
                usdc_size=5000.0,
                timestamp=base + timedelta(hours=30),
                raw={},
            )
        )
        act.append(
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="REDEEM",
                condition_id=cond_loss,
                side=None,
                size=1e4,
                price=None,
                usdc_size=1e4,
                timestamp=base + timedelta(hours=40),
                raw={},
            )
        )
        stats = {
            "pnl_usd": 5e4,
            "win_rate": 0.95,
            "total_trades": 6,
            "trades_per_day": 1.0,
        }
        assert "insider_suspect" not in detect_risk_flags(stats, act, [])

    def test_insider_requires_low_total_trades(self) -> None:
        # Same large near-resolution pattern but a deep history
        # (total_trades > 50) must NOT be flagged.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond = "0xcond_ins3"
        act: list[ActivityRecord] = [
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="TRADE",
                condition_id=cond,
                side="BUY",
                size=2000.0,
                price=0.05,
                usdc_size=8000.0,
                timestamp=base + timedelta(hours=i),
                raw={},
            )
            for i in range(5)
        ]
        act.append(
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="REDEEM",
                condition_id=cond,
                side=None,
                size=1e4,
                price=None,
                usdc_size=1e4,
                timestamp=base + timedelta(hours=20),
                raw={},
            )
        )
        stats = {
            "pnl_usd": 5e4,
            "win_rate": 0.95,
            "total_trades": 200,
            "trades_per_day": 1.0,  # deep history
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
                MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
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
            W_SWING,
            "leaderboard",
            display_name="Swing",
            stats_7d={},
            stats_30d={"pnl_usd": 1.0},
            stats_90d={},
            freq_stats_30d=self._fs(),
            strategy_type=StrategyType.SWING,
            p_score=90.0,
            r_score=80.0,
            f_score=f_score,
            risk_flags=risk_flags,
            last_active_at=None,
        )

    def test_emits_on_new_candidate_crossing_high(self) -> None:
        self._scope(existing=None)
        spy = self._emit_spy()
        asyncio.get_event_loop().run_until_complete(self._upsert(f_score=85.0, risk_flags=[]))
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
        asyncio.get_event_loop().run_until_complete(self._upsert(f_score=42.0, risk_flags=[]))
        spy.assert_not_awaited()

    def test_no_emit_when_already_above_no_recrossing(self) -> None:
        existing = MagicMock()
        existing.sources = ["leaderboard"]
        existing.user_status = "new"
        existing.total_score = 82.0  # already above ⇒ no new crossing
        self._scope(existing=existing)
        spy = self._emit_spy()
        asyncio.get_event_loop().run_until_complete(self._upsert(f_score=88.0, risk_flags=[]))
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
        asyncio.get_event_loop().run_until_complete(self._upsert(f_score=90.0, risk_flags=[]))
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
                    W_SWING,
                    "leaderboard",
                    display_name="X",
                    stats_7d={},
                    stats_30d={"pnl_usd": 1.0},
                    stats_90d={},
                    freq_stats_30d=self._fs(),
                    strategy_type=StrategyType.SWING,
                    p_score=60.0,
                    r_score=50.0,
                    f_score=30.0,
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
                        MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                    ]
                )
                ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                ms.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = MagicMock()

                await _upsert_candidate(
                    W_SWING,
                    "large_new_positions",
                    display_name=None,
                    stats_7d={},
                    stats_30d={"pnl_usd": 2.0},
                    stats_90d={},
                    freq_stats_30d=self._fs(),
                    strategy_type=StrategyType.SWING,
                    p_score=70.0,
                    r_score=60.0,
                    f_score=42.0,
                    risk_flags=[],
                    last_active_at=None,
                )

        asyncio.get_event_loop().run_until_complete(_run())
        # Source merged & deduplicated + sorted; first_discovered_at untouched.
        assert existing.sources == ["large_new_positions", "leaderboard"]
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
                        MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
                        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                    ]
                )
                ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                ms.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = MagicMock()

                await _upsert_candidate(
                    W_SWING,
                    "leaderboard",
                    display_name=None,
                    stats_7d={},
                    stats_30d={},
                    stats_90d={},
                    freq_stats_30d=self._fs(),
                    strategy_type=StrategyType.SWING,
                    p_score=70.0,
                    r_score=60.0,
                    f_score=42.0,
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
            wallet=W_SWING,
            usdc_size=7_500.0,
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
            wallet=W_SWING,
            usdc_size=4_999.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        old_big = _make_record(
            wallet=W_SCALPER,
            usdc_size=50_000.0,
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
            wallet=W_SWING,
            usdc_size=None,
            size=600.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=3),
        )
        small = _make_record(
            wallet=W_SCALPER,
            usdc_size=None,
            size=10.0,
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
                wallet=W_SWING,
                usdc_size=6_000.0,
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
            wallet=W_SWING,
            usdc_size=8_000.0,
            ts=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        ds = _LargePositionStubDS({W_SWING: [big]}, leaderboard_wallets=[W_SWING, W_SCALPER])
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
                    return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
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
                    return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
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
                    return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
                )
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session.add = MagicMock()

                await _run_once(worker)

        asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# Wave B-discovery remediation — P1-9a / P1-9b / P1-9c / I1 / I4 / I7
# ---------------------------------------------------------------------------


class TestP19aUtcDateKeying:
    """P1-9a — ``candidate_score_history`` is keyed by the UTC calendar
    day, not the server's LOCAL date.  A non-UTC ``TZ`` env must NOT shift
    the history row's date.
    """

    _FS = FrequencyStats(
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

    def _capture_history_date(self, tz: str) -> Any:
        """Run _upsert_candidate under ``TZ=tz`` and return the date the
        CandidateScoreHistory row was created with."""
        import datetime as _dt
        import os
        import time

        from polyecho.discovery.worker import _upsert_candidate

        executed: list[Any] = []
        prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = tz
        if hasattr(time, "tzset"):
            time.tzset()
        try:

            async def _run() -> None:
                with patch("polyecho.discovery.worker.session_scope") as ms:
                    sess = AsyncMock()

                    # R10 C1: the history row is written via an atomic
                    # ON CONFLICT statement (execute #2), no longer
                    # ``session.add``.  Capture the executed statements.
                    async def _exec(stmt: Any) -> MagicMock:
                        executed.append(stmt)
                        return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

                    sess.execute = AsyncMock(side_effect=_exec)
                    ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                    ms.return_value.__aexit__ = AsyncMock(return_value=False)
                    sess.add = MagicMock()

                    await _upsert_candidate(
                        W_SWING,
                        "leaderboard",
                        display_name="X",
                        stats_7d={},
                        stats_30d={},
                        stats_90d={},
                        freq_stats_30d=self._FS,
                        strategy_type=StrategyType.SWING,
                        p_score=10.0,
                        r_score=10.0,
                        f_score=5.0,
                        risk_flags=[],
                        last_active_at=None,
                    )

            asyncio.get_event_loop().run_until_complete(_run())
        finally:
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            if hasattr(time, "tzset"):
                time.tzset()

        # execute #1 = candidate SELECT … FOR UPDATE; execute #2 = the
        # history INSERT … ON CONFLICT.  Pull the bound ``date`` value
        # straight out of the history statement's parameters so the
        # assertion checks the EXACT date production keyed the row by.
        assert len(executed) == 2, "expected candidate SELECT + history upsert"
        hist_stmt = executed[1]
        params = hist_stmt.compile().params
        hist_date = params["date"]
        assert isinstance(hist_date, _dt.date), f"history date param is not a date: {hist_date!r}"
        return hist_date

    def test_history_date_is_utc_under_non_utc_tz(self) -> None:
        # Pacific/Kiritimati is UTC+14: its local date can be a day AHEAD
        # of the UTC date.  The history row must still use the UTC day.
        utc_today = datetime.now(tz=UTC).date()
        keyed = self._capture_history_date("Pacific/Kiritimati")
        assert keyed == utc_today

    def test_history_date_is_utc_under_negative_offset_tz(self) -> None:
        # Pacific/Pago_Pago is UTC-11: its local date can be a day BEHIND.
        utc_today = datetime.now(tz=UTC).date()
        keyed = self._capture_history_date("Pacific/Pago_Pago")
        assert keyed == utc_today


class TestP19bWinRateMissingUsdc:
    """P1-9b — a SELL with ``usdc_size is None`` is *missing data* and must
    NOT be counted as a loss in the win_rate denominator.
    """

    def test_none_usdc_sell_excluded_from_denominator(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # 2 priced SELLs: one win (+), one loss (0).  win_rate must be 0.5
        # — the 3rd SELL (usdc_size=None) must NOT drag it to 1/3.
        trades = [
            _make_record(
                side="SELL", usdc_size=100.0, ts=base + timedelta(hours=1), condition_id="a"
            ),
            _make_record(
                side="SELL", usdc_size=0.0, ts=base + timedelta(hours=2), condition_id="b"
            ),
            _make_record(
                side="SELL", usdc_size=None, ts=base + timedelta(hours=3), condition_id="c"
            ),
        ]
        stats = _compute_stats(trades, window_days=30)
        assert stats["win_rate"] == pytest.approx(0.5)

    def test_all_none_usdc_sells_yield_zero_not_div_error(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        trades = [
            _make_record(
                side="SELL", usdc_size=None, ts=base + timedelta(hours=i), condition_id=f"c{i}"
            )
            for i in range(3)
        ]
        stats = _compute_stats(trades, window_days=30)
        # No priced sells → 0.0 (not a ZeroDivisionError, not 0/3 confusion).
        assert stats["win_rate"] == 0.0

    def test_old_behaviour_would_have_deflated(self) -> None:
        """Regression guard: with 1 winning priced SELL + 3 None SELLs the
        OLD code gave 1/4=0.25; the fix gives 1/1=1.0."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        trades = [
            _make_record(side="SELL", usdc_size=500.0, ts=base, condition_id="a"),
            *[
                _make_record(
                    side="SELL", usdc_size=None, ts=base + timedelta(hours=i), condition_id=f"n{i}"
                )
                for i in range(1, 4)
            ],
        ]
        stats = _compute_stats(trades, window_days=30)
        assert stats["win_rate"] == pytest.approx(1.0)


class TestP19cConcurrentHighScoreEmitsOnce:
    """P1-9c — two concurrent same-wallet upserts that both cross the
    high-score threshold must emit CANDIDATE_HIGH_SCORE EXACTLY ONCE.

    The fix takes a ``SELECT … FOR UPDATE`` row lock and performs
    read→decide→emit inside that one transaction.  We model the row lock
    with an ``asyncio.Lock`` shared by both simulated transactions (the
    lock is the test stand-in for Postgres row-level locking, which
    SQLite/mocks do not provide) and assert (a) the production SELECT
    actually requested ``with_for_update`` and (b) the emit fired once.
    """

    _FS = FrequencyStats(
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

    def test_emits_exactly_once_under_race(self) -> None:
        from polyecho.discovery import worker as worker_mod
        from polyecho.discovery.worker import _upsert_candidate

        # Shared "DB row" guarded by a lock — stands in for the candidate
        # row + the Postgres FOR UPDATE row lock that SQLite/mocks lack.
        # The lock is acquired on the candidate SELECT and released only
        # when the transaction exits (commits), exactly serialising the
        # two concurrent runs the way SELECT … FOR UPDATE does.
        row_lock = asyncio.Lock()
        shared: dict[str, Any] = {"total_score": None}
        emit_spy = AsyncMock()

        def _fake_candidate(score: float) -> MagicMock:
            # Permissive ORM-row double: the update branch freely assigns
            # last_active_at / stats_* / etc.; only the fields the race
            # logic READS need controlled values.
            c = MagicMock()
            c.sources = ["leaderboard"]
            c.user_status = "new"
            c.total_score = score
            c.first_discovered_at = object()
            return c

        def _fake_scope() -> Any:
            cm = MagicMock()
            sess = AsyncMock()
            # Per-transaction state (closure-local → no cross-coroutine
            # races): execute call index + whether we hold the row lock.
            state = {"calls": 0, "locked": False}

            async def _execute(stmt: Any) -> MagicMock:
                state["calls"] += 1
                if state["calls"] == 1:
                    # Candidate SELECT … FOR UPDATE → take the row lock
                    # (blocks the concurrent run until we commit).
                    await row_lock.acquire()
                    state["locked"] = True
                    score = shared["total_score"]
                    existing = _fake_candidate(score) if score is not None else None
                    return MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
                # Score-history SELECT (no existing row).
                return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

            def _add(obj: Any) -> None:
                # Commit-equivalent: persist total_score so the NEXT
                # serialised transaction reads it as prev_score.
                if type(obj).__name__ == "CandidateLeader":
                    ts = getattr(obj, "total_score", None)
                    if ts is not None:
                        shared["total_score"] = float(ts)

            sess.execute = AsyncMock(side_effect=_execute)
            sess.add = _add

            async def _aexit(*_a: Any) -> bool:
                # Transaction end == commit + release the row lock.
                if state["locked"]:
                    row_lock.release()
                    state["locked"] = False
                return False

            cm.__aenter__ = AsyncMock(return_value=sess)
            cm.__aexit__ = AsyncMock(side_effect=_aexit)
            return cm

        async def _one() -> None:
            await _upsert_candidate(
                W_SWING,
                "leaderboard",
                display_name="Swing",
                stats_7d={},
                stats_30d={"pnl_usd": 1.0},
                stats_90d={},
                freq_stats_30d=self._FS,
                strategy_type=StrategyType.SWING,
                p_score=90.0,
                r_score=80.0,
                f_score=85.0,  # crosses 70
                risk_flags=[],
                last_active_at=None,
            )

        async def _run() -> None:
            with (
                patch.object(worker_mod, "session_scope", _fake_scope),
                patch.object(worker_mod, "emit_event", emit_spy),
            ):
                # Two concurrent runs for the SAME wallet.
                await asyncio.gather(_one(), _one())

        asyncio.get_event_loop().run_until_complete(_run())

        assert emit_spy.await_count == 1, (
            f"expected exactly one CANDIDATE_HIGH_SCORE emit, got {emit_spy.await_count}"
        )

    def test_select_requests_for_update_lock(self) -> None:
        """The candidate SELECT must carry a FOR UPDATE clause (the
        mechanism that makes the race fix atomic)."""
        from polyecho.discovery.worker import _upsert_candidate

        captured: list[Any] = []

        async def _run() -> None:
            with patch("polyecho.discovery.worker.session_scope") as ms:
                sess = AsyncMock()

                async def _exec(stmt: Any) -> MagicMock:
                    captured.append(stmt)
                    return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

                sess.execute = AsyncMock(side_effect=_exec)
                ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                ms.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = MagicMock()

                await _upsert_candidate(
                    W_SWING,
                    "leaderboard",
                    display_name="X",
                    stats_7d={},
                    stats_30d={},
                    stats_90d={},
                    freq_stats_30d=self._FS,
                    strategy_type=StrategyType.SWING,
                    p_score=10.0,
                    r_score=10.0,
                    f_score=5.0,
                    risk_flags=[],
                    last_active_at=None,
                )

        asyncio.get_event_loop().run_until_complete(_run())
        assert captured, "no statement was executed"
        # The candidate SELECT is the first execute; it must be FOR UPDATE.
        sel = captured[0]
        compiled = str(sel).upper()
        assert "FOR UPDATE" in compiled, f"candidate SELECT is not FOR UPDATE:\n{sel}"


class TestI1RetrySleepDistinctFromInterval:
    """I1 — a failed loop iteration must sleep a SHORT ``retry_sleep``
    (60 s, exponential, capped), NOT the full 1d/7d ``interval``.
    """

    def test_exception_sleeps_short_retry_not_full_interval(self) -> None:
        """Drive the inner ``_loop`` directly: an exception path must sleep
        the short retry, NEVER the long interval.  We rebuild the same
        loop the module uses (the closure is private) so the assertion is
        deterministic and free of task-scheduling races.
        """
        from polyecho.discovery import __main__ as m

        sleeps: list[float] = []
        calls = {"n": 0}

        async def _flaky() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("DB not ready at startup")
            # Second iteration: succeed once (resets backoff), then stop.
            if calls["n"] == 2:
                return
            raise asyncio.CancelledError

        async def _fake_sleep(secs: float) -> None:
            sleeps.append(secs)
            # Let the loop continue until _flaky raises CancelledError.

        async def _loop_like_main(interval: timedelta) -> None:
            """Byte-for-byte the loop body shipped in __main__._loop."""
            retry_sleep = m._RETRY_SLEEP_INITIAL
            while True:
                try:
                    await _flaky()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await _fake_sleep(retry_sleep.total_seconds())
                    retry_sleep = min(retry_sleep * 2, m._RETRY_SLEEP_MAX)
                    continue
                retry_sleep = m._RETRY_SLEEP_INITIAL
                await _fake_sleep(interval.total_seconds())

        async def _run() -> None:
            with pytest.raises(asyncio.CancelledError):
                await _loop_like_main(m._DAILY_INTERVAL)

        asyncio.get_event_loop().run_until_complete(_run())

        daily_interval_s = m._DAILY_INTERVAL.total_seconds()
        retry_s = m._RETRY_SLEEP_INITIAL.total_seconds()
        assert sleeps, "loop never slept"
        # First sleep = the short retry after the RuntimeError, NOT 24h.
        assert sleeps[0] == retry_s
        assert sleeps[0] < daily_interval_s
        # After the success (2nd call) the loop resumes the long cadence.
        assert daily_interval_s in sleeps
        assert sleeps.index(retry_s) < sleeps.index(daily_interval_s)

    def test_scheduled_loop_catches_and_uses_retry_sleep(self) -> None:
        """Integration: the real ``_scheduled_loop`` must survive a task
        exception and use the short retry (not dark-out for a day)."""
        from polyecho.discovery import __main__ as m

        sleeps: list[float] = []
        n = {"v": 0}

        async def _flaky() -> None:
            n["v"] += 1
            if n["v"] == 1:
                raise RuntimeError("startup blip")
            raise asyncio.CancelledError

        async def _fake_sleep(secs: float) -> None:
            sleeps.append(secs)

        async def _run() -> None:
            worker = MagicMock()
            worker.run_daily = _flaky
            worker.run_hourly = AsyncMock(side_effect=asyncio.CancelledError)
            with patch("polyecho.discovery.__main__.asyncio.sleep", _fake_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await m._scheduled_loop(worker)

        asyncio.get_event_loop().run_until_complete(_run())
        # The daily task hit RuntimeError and recorded the SHORT retry
        # sleep; the full 24h interval must never have been slept.
        assert m._RETRY_SLEEP_INITIAL.total_seconds() in sleeps
        assert m._DAILY_INTERVAL.total_seconds() not in sleeps

    def test_retry_sleep_constants_are_short_and_capped(self) -> None:
        from polyecho.discovery import __main__ as m

        assert m._RETRY_SLEEP_INITIAL < m._HOURLY_INTERVAL
        assert m._RETRY_SLEEP_INITIAL < m._DAILY_INTERVAL
        assert m._RETRY_SLEEP_MAX <= m._HOURLY_INTERVAL
        assert m._RETRY_SLEEP_INITIAL.total_seconds() == 60.0


class TestI4LargePositionNoTruncation:
    """I4 — the hourly large-position scan must NOT truncate a burst
    trader's single largest fill via an arbitrary page ``limit``.
    """

    def test_largest_fill_not_truncated_when_many_trades(self) -> None:
        ts = datetime.now(tz=UTC) - timedelta(hours=2)
        # 250 small trades + ONE huge one at the END (would be dropped by
        # the old limit=100 timestamp-ordered fetch).
        small = [
            _make_record(wallet=W_SWING, usdc_size=10.0, ts=ts + timedelta(seconds=i))
            for i in range(250)
        ]
        huge = _make_record(
            wallet=W_SWING,
            usdc_size=80_000.0,
            ts=ts + timedelta(seconds=500),
        )
        ds = _LargePositionStubDS({W_SWING: [*small, huge]})
        seen_limits: list[int | None] = []

        # Wrap to record the limit the worker passes down.
        orig = ds.get_user_activity

        async def _spy(wallet: str, **kw: Any) -> list[ActivityRecord]:
            seen_limits.append(kw.get("limit"))
            return await orig(wallet, **kw)

        ds.get_user_activity = _spy  # type: ignore[method-assign]
        processed: list[str] = []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)  # type: ignore[arg-type]
            worker._known_wallets = {W_SWING}

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append(w)

            with patch.object(worker, "_process_wallet", _fake_process):
                await worker.discover_large_new_positions()

        asyncio.get_event_loop().run_until_complete(_run())
        # Wallet detected despite the huge fill being the 251st record.
        assert processed == [W_SWING]
        # The scan requests the FULL window (limit=None), not a small cap.
        assert seen_limits and all(lim is None for lim in seen_limits)

    def test_offset_cap_constant_matches_data_api(self) -> None:
        from polyecho.discovery.worker import (
            _LARGE_POSITION_DATA_API_OFFSET_CAP,
        )

        # Polymarket data-api GET /activity: offset max 10000 (context7).
        assert _LARGE_POSITION_DATA_API_OFFSET_CAP == 10_000


class TestI7BurstinessDefinitionConsistent:
    """I7 / Z1 — burstiness is a SATURATING-normalised CV ``cv/(1+cv)``
    in [0, 1): its docstring no longer falsely cites Goh-Barabási, and —
    critically — it NEVER pins at exactly 1.0 for finite CV, so the
    downstream multiplicative replicability penalty ``1 − burstiness``
    can no longer hard-zero every leader's score (the prior
    ``clip(cv,0,1)`` did exactly that — see
    TestScoringCollapseRegression).
    """

    def test_burstiness_high_but_strictly_below_one_when_clustered(
        self,
    ) -> None:
        # Extremely clustered (8 trades 1 s apart + one 20-day gap) →
        # very high CV.  The OLD ``clip(cv,0,1)`` pinned this at EXACTLY
        # 1.0 (→ replicability factor 0 → all scores 0).  The saturating
        # ``cv/(1+cv)`` must instead stay high yet STRICTLY < 1.0.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        recs: list[ActivityRecord] = []
        for i in range(8):  # tight cluster
            recs.append(_make_record(ts=base + timedelta(seconds=i), condition_id="a"))
        recs.append(_make_record(ts=base + timedelta(days=20), condition_id="b"))  # one huge gap
        stats = compute_frequency_stats(recs, window_days=30)
        assert 0.0 <= stats.burstiness < 1.0  # never the clip 1.0
        assert stats.burstiness > 0.5  # still high (bursty)

    def test_downstream_factor_well_defined(self) -> None:
        # For any burstiness in [0, 1], 1 - burstiness ∈ [0, 1] so the
        # replicability score stays bounded and monotone.
        for b in (0.0, 0.25, 0.5, 0.75, 1.0):
            fs = {
                "median_gap_minutes": 60.0,
                "burstiness": b,
                "active_hours_utc": list(range(0, 15)),
                "concurrent_positions_avg": 1.0,
            }
            score = replicability_score(fs, follower_capital_usd=1000.0)
            assert 0.0 <= score <= 100.0
        # Strictly decreasing in burstiness (penalty direction correct).
        low = replicability_score(
            {
                "median_gap_minutes": 60.0,
                "burstiness": 0.1,
                "active_hours_utc": list(range(0, 15)),
                "concurrent_positions_avg": 1.0,
            },
            follower_capital_usd=1000.0,
        )
        high = replicability_score(
            {
                "median_gap_minutes": 60.0,
                "burstiness": 0.9,
                "active_hours_utc": list(range(0, 15)),
                "concurrent_positions_avg": 1.0,
            },
            follower_capital_usd=1000.0,
        )
        assert low > high

    def test_docstring_no_longer_claims_goh_barabasi_impl(self) -> None:
        import polyecho.discovery.frequency as fq

        doc = fq.compute_frequency_stats.__doc__ or ""
        # The fix must explicitly disclaim the Goh-Barabási index and
        # state it is a clipped coefficient of variation.
        assert "coefficient of variation" in doc.lower()
        assert "not" in doc.lower() and "goh" in doc.lower()


# ===========================================================================
# 20-agent review (R10) remediation — Wave E-discovery
# ===========================================================================


class TestR10I1TimeOverlapDedup:
    """R10 I1 — ``time_overlap`` denominator must use DISTINCT active
    hours.  A non-deduped ``active_hours`` list must NOT inflate the
    denominator and deflate the replicability score.
    """

    def test_duplicate_active_hours_do_not_deflate_score(self) -> None:
        # 4 distinct hours {0,1,2,3} all inside the follower window
        # (00–14 UTC) → time_overlap must be 1.0 regardless of how many
        # times each hour is repeated in the input list.
        deduped = {
            "median_gap_minutes": 60.0,
            "burstiness": 0.0,
            "active_hours_utc": [0, 1, 2, 3],
            "concurrent_positions_avg": 1.0,
        }
        non_deduped = dict(deduped)
        non_deduped["active_hours_utc"] = [0, 0, 0, 1, 1, 2, 2, 2, 2, 3]

        s_dedup = replicability_score(deduped, follower_capital_usd=1000.0)
        s_nondedup = replicability_score(non_deduped, follower_capital_usd=1000.0)

        # Old code: 4/10 = 0.4 overlap → score deflated.  Fixed: both
        # collapse to the 4 distinct hours → identical, non-deflated.
        assert s_dedup == s_nondedup
        assert s_dedup > 0.0

    def test_partial_overlap_uses_distinct_denominator(self) -> None:
        # Distinct hours {0, 1, 20, 21}: 2 of 4 inside the 00–14 window.
        fs = {
            "median_gap_minutes": 60.0,
            "burstiness": 0.0,
            "active_hours_utc": [0, 0, 1, 20, 20, 20, 21],
            "concurrent_positions_avg": 1.0,
        }
        # freq=1.0, capital=1.0, burst=1.0, overlap=2/4=0.5 → 50.0
        assert replicability_score(fs, 1000.0) == pytest.approx(50.0)


class TestR10I3NearResolutionSymmetricCounting:
    """R10 I3 — near-resolution win-rate must count wins and total at
    the SAME granularity (per-condition).  The old code added wins
    per-condition but total per-SELL-trade.
    """

    def test_multiple_near_res_sells_one_condition_count_once(self) -> None:
        """Many losing SELLs on ONE condition must count that condition
        ONCE toward the total (per-condition), not once per SELL trade
        (which made the denominator explode and could never flag)."""
        from polyecho.discovery import riskflags as rf

        base = datetime(2026, 1, 1, tzinfo=UTC)
        cond = "0xsym_cond"
        # 5 near-resolution BUYs + REDEEM → exactly ONE win-condition,
        # so near_res_total=1, near_res_wins=1 → wr = 1.0 ≥ 0.80.
        act: list[ActivityRecord] = [
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="TRADE",
                condition_id=cond,
                side="BUY",
                size=2000.0,
                price=0.05,
                usdc_size=8000.0,
                timestamp=base + timedelta(hours=i),
                raw={},
            )
            for i in range(5)
        ]
        # 3 SELLs on the SAME condition in the near-res window.  Old
        # code: near_res_total += 3 → wr = 1/4 = 0.25 (never flags).
        # Fixed: same condition already counted; still 1/1 = 1.0.
        for h in (15, 16, 17):
            act.append(
                ActivityRecord(
                    wallet=W_INSIDER,
                    activity_type="TRADE",
                    condition_id=cond,
                    side="SELL",
                    size=10.0,
                    price=0.9,
                    usdc_size=6000.0,
                    timestamp=base + timedelta(hours=h),
                    raw={},
                )
            )
        act.append(
            ActivityRecord(
                wallet=W_INSIDER,
                activity_type="REDEEM",
                condition_id=cond,
                side=None,
                size=1e4,
                price=None,
                usdc_size=1e4,
                timestamp=base + timedelta(hours=20),
                raw={},
            )
        )
        stats = {
            "pnl_usd": 5e4,
            "win_rate": 0.95,
            "total_trades": 8,
            "trades_per_day": 1.0,
        }
        flags = rf.detect_risk_flags(stats, act, [])
        # Symmetric per-condition: wr = 1/1 = 1.0 ≥ 0.80 → flagged.
        assert "insider_suspect" in flags


class TestR10I4NinetyDayWindowing:
    """R10 I4 — the 90d stats must be computed on a 90d-WINDOWED slice,
    not the raw unwindowed activity (which understates trades_per_day if
    the datasource returns >90d).
    """

    def test_stats_90d_windowed_in_process_wallet(self) -> None:
        from polyecho.discovery import worker as wk

        captured: dict[str, Any] = {}

        async def _fake_upsert(*a: Any, **kw: Any) -> tuple[bool, datetime | None]:
            captured["stats_90d"] = kw["stats_90d"]
            captured["stats_30d"] = kw["stats_30d"]
            # Phase-3: _upsert_candidate now returns
            # (emitted_high_score, prior_last_seen_ts) which
            # _process_wallet unpacks — match that contract.
            return False, None

        now = datetime.now(tz=UTC)
        # 10 trades inside the last 90d + 40 trades 200 days ago (must be
        # excluded from the 90d window).
        recent = [
            _make_record(
                side="BUY", usdc_size=100.0, ts=now - timedelta(days=d), condition_id=f"r{d}"
            )
            for d in range(10)
        ]
        ancient = [
            _make_record(
                side="BUY", usdc_size=100.0, ts=now - timedelta(days=200 + d), condition_id=f"a{d}"
            )
            for d in range(40)
        ]

        class _DS:
            async def get_user_activity(
                self,
                w: str,
                *,
                activity_type: str,
                limit: Any = None,
                start: Any = None,
            ) -> list[ActivityRecord]:
                if activity_type == "TRADE":
                    return recent + ancient
                return []

            async def get_positions(
                self,
                w: str,
                *,
                size_threshold: float = 0.0,
            ) -> list[PositionRecord]:
                return []

        async def _run() -> None:
            with patch.object(wk, "_upsert_candidate", _fake_upsert):
                worker = DiscoveryWorker(_DS())  # type: ignore[arg-type]
                await worker._process_wallet(W_SWING, "leaderboard")

        asyncio.get_event_loop().run_until_complete(_run())

        # Only the 10 recent trades fall inside the 90d window; the 40
        # ancient ones must be excluded.  trades_per_day = 10/90.
        assert captured["stats_90d"]["total_trades"] == 10
        assert captured["stats_90d"]["trades_per_day"] == pytest.approx(10 / 90, rel=1e-3)


class TestR10C1C2HistoryRaceAndLockRelease:
    """R10 C1 — concurrent same-wallet+same-UTC-day runs must yield
    EXACTLY ONE history row (atomic ON CONFLICT), and R10 C2 — the
    CANDIDATE_HIGH_SCORE emit must happen AFTER the txn/lock is released.
    """

    _FS = FrequencyStats(
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

    def test_history_write_is_on_conflict_upsert(self) -> None:
        """The history write must be a single INSERT … ON CONFLICT
        (wallet_address, date) DO UPDATE — atomic under concurrency, no
        unlocked SELECT-then-add."""
        from polyecho.discovery.worker import _upsert_candidate

        executed: list[Any] = []

        async def _run() -> None:
            with patch("polyecho.discovery.worker.session_scope") as ms:
                sess = AsyncMock()

                async def _exec(stmt: Any) -> MagicMock:
                    executed.append(stmt)
                    return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

                sess.execute = AsyncMock(side_effect=_exec)
                ms.return_value.__aenter__ = AsyncMock(return_value=sess)
                ms.return_value.__aexit__ = AsyncMock(return_value=False)
                sess.add = MagicMock()

                await _upsert_candidate(
                    W_SWING,
                    "leaderboard",
                    display_name="X",
                    stats_7d={},
                    stats_30d={"pnl_usd": 1.0},
                    stats_90d={},
                    freq_stats_30d=self._FS,
                    strategy_type=StrategyType.SWING,
                    p_score=10.0,
                    r_score=10.0,
                    f_score=5.0,
                    risk_flags=[],
                    last_active_at=None,
                )

        asyncio.get_event_loop().run_until_complete(_run())

        # No ORM row added for history; it is an executed ON CONFLICT.
        assert len(executed) == 2
        compiled = str(executed[1].compile()).upper()
        assert "ON CONFLICT" in compiled
        assert "DO UPDATE" in compiled
        assert "CANDIDATE_SCORE_HISTORY" in compiled

    def test_concurrent_same_wallet_one_history_upsert_one_emit(self) -> None:
        """Two concurrent same-wallet runs crossing the threshold: the
        history upsert is idempotent (ON CONFLICT, so no dup-key even
        without serialisation) AND the emit fires exactly once AND every
        emit happens strictly AFTER its txn's lock was released."""
        from polyecho.discovery import worker as worker_mod
        from polyecho.discovery.worker import _upsert_candidate

        row_lock = asyncio.Lock()
        shared: dict[str, Any] = {"total_score": None}
        emit_spy = AsyncMock()
        # Records, in order, events: ("lock"/"unlock"/"emit").
        timeline: list[str] = []
        hist_upserts = {"n": 0}

        async def _spy_emit(**_kw: Any) -> None:
            timeline.append("emit")

        def _fake_scope() -> Any:
            cm = MagicMock()
            sess = AsyncMock()
            state = {"calls": 0, "locked": False}

            async def _execute(stmt: Any) -> MagicMock:
                state["calls"] += 1
                if state["calls"] == 1:
                    await row_lock.acquire()
                    state["locked"] = True
                    timeline.append("lock")
                    score = shared["total_score"]
                    existing = None
                    if score is not None:
                        existing = MagicMock()
                        existing.sources = ["leaderboard"]
                        existing.user_status = "new"
                        existing.total_score = score
                        existing.first_discovered_at = object()
                    return MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
                # Second execute = history ON CONFLICT upsert (idempotent
                # — count it; it never raises a dup-key by construction).
                hist_upserts["n"] += 1
                return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

            def _add(obj: Any) -> None:
                if type(obj).__name__ == "CandidateLeader":
                    ts = getattr(obj, "total_score", None)
                    if ts is not None:
                        shared["total_score"] = float(ts)

            sess.execute = AsyncMock(side_effect=_execute)
            sess.add = _add

            async def _aexit(*_a: Any) -> bool:
                if state["locked"]:
                    timeline.append("unlock")
                    row_lock.release()
                    state["locked"] = False
                return False

            cm.__aenter__ = AsyncMock(return_value=sess)
            cm.__aexit__ = AsyncMock(side_effect=_aexit)
            return cm

        async def _one() -> None:
            await _upsert_candidate(
                W_SWING,
                "leaderboard",
                display_name="Swing",
                stats_7d={},
                stats_30d={"pnl_usd": 1.0},
                stats_90d={},
                freq_stats_30d=self._FS,
                strategy_type=StrategyType.SWING,
                p_score=90.0,
                r_score=80.0,
                f_score=85.0,  # crosses 70
                risk_flags=[],
                last_active_at=None,
            )

        async def _run() -> None:
            with (
                patch.object(worker_mod, "session_scope", _fake_scope),
                patch.object(worker_mod, "_safe_emit_high_score", _spy_emit),
                patch.object(worker_mod, "emit_event", emit_spy),
            ):
                await asyncio.gather(_one(), _one())

        asyncio.get_event_loop().run_until_complete(_run())

        # Two runs → two idempotent history upserts (ON CONFLICT makes a
        # duplicate a no-op UPDATE, never a dup-key error or dup row).
        assert hist_upserts["n"] == 2
        # Exactly one emit (second run sees prev_score ≥ threshold).
        assert timeline.count("emit") == 1
        # R10 C2: every emit must come strictly AFTER an unlock — never
        # while a FOR UPDATE lock is held.  No "emit" may appear between
        # a "lock" and its matching "unlock".
        held = 0
        for ev in timeline:
            if ev == "lock":
                held += 1
            elif ev == "unlock":
                held -= 1
            elif ev == "emit":
                assert held == 0, f"emit happened while a row lock was held: {timeline}"


# ---------------------------------------------------------------------------
# Idle keepalive heartbeat — false-stale fix
#
# The daily cadence loop heartbeats once per pass then sleeps ~24 h, so a
# healthy-but-sleeping discovery worker would write no heartbeat for a day
# and ``/status`` would false-flag it 逾時/stale after 5 min.  A dedicated
# ~90 s ``phase="idle"`` keepalive task closes that gap via the SAME
# fail-soft ``_emit_discovery_heartbeat`` the real passes use.
#
# Deterministic + network-free: no wall-clock sleeps — ``asyncio.sleep`` is
# monkeypatched to an Event-gated fake so the loop is driven iteration by
# iteration, then cancelled.
# ---------------------------------------------------------------------------


class TestDiscoveryIdleKeepalive:
    """``_idle_keepalive_loop`` keeps a sleeping discovery worker fresh."""

    @staticmethod
    def _stub_worker() -> Any:
        """A DB-free worker double exposing only what the keepalive needs.

        ``_emit_discovery_heartbeat`` reads ``drain_pass_counters`` /
        ``datasource_name``; everything else is irrelevant to the idle path.
        """
        worker = MagicMock()
        worker.drain_pass_counters = MagicMock(return_value={})
        worker.datasource_name = "FixtureDataSource"
        return worker

    def test_idle_keepalive_emits_idle_heartbeat_so_not_stale(self) -> None:
        """A parked/scheduled worker emits a SUBSEQUENT ``phase="idle"``
        heartbeat on the keepalive cadence — so ``/status`` would NOT see
        it as stale between two ~24 h daily passes.

        Deterministic (mirrors the core ``test_task2`` pattern): the ~90 s
        cadence is shrunk to ~1 ms and every captured beat sets an Event so
        the loop is advanced cycle-by-cycle with no wall-clock flakiness.
        """
        from datetime import timedelta

        from polyecho.discovery import __main__ as m

        worker = self._stub_worker()
        captured: list[str] = []
        tick = asyncio.Event()

        async def _capture_hb(_w: Any, *, phase: str) -> None:
            captured.append(phase)
            tick.set()

        async def _run() -> None:
            # Shrink the real ~90 s cadence so the loop ticks fast in-test;
            # the SHIPPED value is asserted separately below.
            with (
                patch.object(m, "_emit_discovery_heartbeat", _capture_hb),
                patch.object(m, "_IDLE_KEEPALIVE_INTERVAL", timedelta(seconds=0.001)),
            ):
                task = asyncio.create_task(m._idle_keepalive_loop(worker))
                try:
                    for expected in (1, 2, 3):
                        await asyncio.wait_for(tick.wait(), timeout=5.0)
                        tick.clear()
                        assert len(captured) >= expected
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        asyncio.get_event_loop().run_until_complete(_run())

        # A subsequent heartbeat WAS emitted, and every one is phase="idle".
        assert len(captured) >= 3
        assert all(p == "idle" for p in captured)
        # The SHIPPED cadence (~90 s) is well under the 300 s stale window
        # (``HEARTBEAT_STALE_SECONDS``) — proving a sleeping worker keeps
        # reporting in time WITHOUT loosening that threshold.
        shipped = m._IDLE_KEEPALIVE_INTERVAL.total_seconds()
        assert 0 < shipped <= 90.0
        assert shipped < 300.0

    def test_idle_keepalive_failsoft_backend_does_not_abort_worker(
        self,
    ) -> None:
        """A raising heartbeat backend must NOT abort the keepalive loop.

        Drives the REAL ``_emit_discovery_heartbeat`` (and thus the real
        ``emit_heartbeat_safe`` seam) with a shared ``write_heartbeat`` that
        always raises — the loop must keep ticking, never propagate the
        error, and still stop cleanly on cancellation.
        """
        from datetime import timedelta

        from polyecho.discovery import __main__ as m

        worker = self._stub_worker()
        ticks = {"n": 0}
        gate = asyncio.Event()

        def _boom_write_heartbeat(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("settings table unreachable")

        fake_health = types.ModuleType("polyecho.shared.health")
        fake_health.write_heartbeat = _boom_write_heartbeat  # type: ignore[attr-defined]

        # Count each REAL fail-soft heartbeat attempt by spying the worker's
        # drained counters (called once per ``_emit_discovery_heartbeat``).
        def _drain() -> dict[str, int]:
            ticks["n"] += 1
            gate.set()
            return {}

        worker.drain_pass_counters = MagicMock(side_effect=_drain)

        async def _run() -> None:
            saved = sys.modules.get("polyecho.shared.health")
            sys.modules["polyecho.shared.health"] = fake_health
            try:
                with patch.object(m, "_IDLE_KEEPALIVE_INTERVAL", timedelta(seconds=0.001)):
                    task = asyncio.create_task(m._idle_keepalive_loop(worker))
                    try:
                        # Several iterations despite EVERY heartbeat raising.
                        for _ in range(3):
                            await asyncio.wait_for(gate.wait(), timeout=5.0)
                            gate.clear()
                        # Loop is alive (not crashed) and cancellable.
                        assert not task.done()
                    finally:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            finally:
                if saved is not None:
                    sys.modules["polyecho.shared.health"] = saved
                else:
                    sys.modules.pop("polyecho.shared.health", None)

        asyncio.get_event_loop().run_until_complete(_run())

        # The fail-soft seam swallowed every raise; the loop kept ticking
        # and ended only via cooperative cancellation.
        assert ticks["n"] >= 3

    def test_idle_keepalive_cancels_cleanly_on_shutdown(self) -> None:
        """Cooperative shutdown cancels the keepalive promptly (graceful
        ``CancelledError`` is propagated, never swallowed)."""
        from datetime import timedelta

        from polyecho.discovery import __main__ as m

        worker = self._stub_worker()
        started = asyncio.Event()

        async def _hb(_w: Any, *, phase: str) -> None:
            started.set()

        async def _run() -> None:
            with (
                patch.object(m, "_emit_discovery_heartbeat", _hb),
                patch.object(m, "_IDLE_KEEPALIVE_INTERVAL", timedelta(seconds=0.001)),
            ):
                task = asyncio.create_task(m._idle_keepalive_loop(worker))
                await asyncio.wait_for(started.wait(), timeout=5.0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert task.cancelled()

        asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# 防再種 — fixture mode must NOT auto-seed the operator DB
# ---------------------------------------------------------------------------
class TestFixturePersistenceGate:
    """The deployed discovery entrypoint must NOT persist the synthetic
    fixture leaderboard into ``candidate_leaders`` in fixture-datasource
    mode, so a live operator DB is never auto-polluted with fake leaders.
    This is UNCONDITIONAL — there is no opt-in to write fixture data
    (不再有任何產生假資料的邏輯).

    The gate defaults OPEN and is flipped ONLY by ``__main__.main()``;
    every other test drives the worker/upsert helpers directly (their own
    mocked ``session_scope``), so those legitimately keep exercising
    persistence — these tests pin BOTH halves of that contract.
    """

    _FS = FrequencyStats(
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

    @contextlib.contextmanager
    def _gate(self, enabled: bool):  # type: ignore[no-untyped-def]
        """Set the module gate, ALWAYS restoring the open default after —
        a leaked global would corrupt every later persistence test."""
        from polyecho.discovery import worker as w

        try:
            w.set_persistence_enabled(enabled)
            yield
        finally:
            w.set_persistence_enabled(True)

    # -- gate default --------------------------------------------------------
    def test_gate_defaults_open(self) -> None:
        """Regression: the gate MUST default open so the entire existing
        suite keeps exercising fixture-data persistence unchanged."""
        from polyecho.discovery.worker import persistence_enabled

        assert persistence_enabled() is True

    # -- worker write functions honour a CLOSED gate -------------------------
    def test_upsert_candidate_skips_db_when_gate_closed(self) -> None:
        from polyecho.discovery.worker import _upsert_candidate

        scope_calls = {"n": 0}

        async def _run() -> None:
            with self._gate(False):
                with patch("polyecho.discovery.worker.session_scope") as ms:
                    ms.side_effect = lambda *a, **k: scope_calls.__setitem__(
                        "n", scope_calls["n"] + 1
                    )
                    emitted, prior = await _upsert_candidate(
                        W_SWING,
                        "leaderboard",
                        display_name="X",
                        stats_7d={},
                        stats_30d={"pnl_usd": 1.0},
                        stats_90d={},
                        freq_stats_30d=self._FS,
                        strategy_type=StrategyType.SWING,
                        p_score=99.0,
                        r_score=99.0,
                        f_score=99.0,
                        risk_flags=[],
                        last_active_at=None,
                    )
            # Brand-new-wallet sentinel — callers must see "not in DB yet".
            assert emitted is False
            assert prior is None

        asyncio.get_event_loop().run_until_complete(_run())
        # session_scope (the DB write boundary) was NEVER opened.
        assert scope_calls["n"] == 0

    def test_upsert_thin_skips_db_when_gate_closed(self) -> None:
        from polyecho.discovery.worker import _upsert_thin

        scope_calls = {"n": 0}

        async def _run() -> tuple[Any, Any, Any, Any]:
            with self._gate(False):
                with patch("polyecho.discovery.worker.session_scope") as ms:
                    ms.side_effect = lambda *a, **k: scope_calls.__setitem__(
                        "n", scope_calls["n"] + 1
                    )
                    return await _upsert_thin(
                        W_SWING,
                        "leaderboard",
                        display_name="X",
                        rank=1,
                        leaderboard_pnl=123.0,
                        leaderboard_volume=456.0,
                    )

        out = asyncio.get_event_loop().run_until_complete(_run())
        # Brand-new-wallet sentinel (None, None, None, {}) and no DB write.
        assert out == (None, None, None, {})
        assert scope_calls["n"] == 0

    def test_insert_drift_event_skips_db_when_gate_closed(self) -> None:
        from polyecho.discovery.worker import _insert_drift_event

        scope_calls = {"n": 0}

        async def _run() -> None:
            with self._gate(False):
                with patch("polyecho.discovery.worker.session_scope") as ms:
                    ms.side_effect = lambda *a, **k: scope_calls.__setitem__(
                        "n", scope_calls["n"] + 1
                    )
                    await _insert_drift_event(W_SWING, "swing", "scalper")

        asyncio.get_event_loop().run_until_complete(_run())
        assert scope_calls["n"] == 0

    def test_open_gate_still_persists(self) -> None:
        """Non-vacuity: with the gate OPEN the SAME call DOES open
        session_scope — proving the skip is the gate, not the mock."""
        from polyecho.discovery.worker import _upsert_thin

        opened = {"n": 0}

        async def _run() -> None:
            with self._gate(True):
                with patch("polyecho.discovery.worker.session_scope") as ms:
                    sess = AsyncMock()
                    sess.execute = AsyncMock(
                        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
                    )
                    sess.add = MagicMock()

                    def _enter(*_a: Any, **_k: Any) -> Any:
                        opened["n"] += 1
                        cm = MagicMock()
                        cm.__aenter__ = AsyncMock(return_value=sess)
                        cm.__aexit__ = AsyncMock(return_value=False)
                        return cm

                    ms.side_effect = _enter
                    await _upsert_thin(
                        W_SWING,
                        "leaderboard",
                        display_name="X",
                        rank=1,
                        leaderboard_pnl=1.0,
                        leaderboard_volume=2.0,
                    )

        asyncio.get_event_loop().run_until_complete(_run())
        assert opened["n"] == 1

    # -- __main__.main() is the boundary that flips the gate -----------------
    def test_main_fixture_no_optin_disables_persistence(self) -> None:
        """``main(once=True)`` under a REAL FixtureDataSource (no opt-in)
        runs the full pipeline but the candidate-WRITE boundary is closed.

        NOTE: ``session_scope`` is shared infra — the hourly L2 cursor
        READ (``_read_cursors``) legitimately opens it and is NOT a
        pollution path, so the precise invariant is "the gate is closed
        for the deployed pass AND the three candidate-WRITE helpers
        short-circuit", NOT "no session is ever opened".
        """
        import os

        from polyecho.discovery import __main__ as m
        from polyecho.discovery import worker as w
        from polyecho.shared.config import get_settings
        from polyecho.shared.datasource import FixtureDataSource

        gate_during_pass: dict[str, bool] = {}
        upsert_thin_writes = {"n": 0}
        upsert_cand_writes = {"n": 0}

        real_run_once = m._run_once

        async def _run_once_capture(worker: Any) -> None:
            # The gate the *deployed pass* actually observes.
            gate_during_pass["enabled"] = w.persistence_enabled()
            await real_run_once(worker)

        # Wrap the real write helpers: count only calls that get PAST the
        # gate into the actual DB-write body (session_scope opened).
        real_thin = w._upsert_thin
        real_cand = w._upsert_candidate

        async def _thin_spy(*a: Any, **k: Any):  # type: ignore[no-untyped-def]
            with patch("polyecho.discovery.worker.session_scope") as ms:
                ms.side_effect = lambda *x, **y: upsert_thin_writes.__setitem__(
                    "n", upsert_thin_writes["n"] + 1
                )
                return await real_thin(*a, **k)

        async def _cand_spy(*a: Any, **k: Any):  # type: ignore[no-untyped-def]
            with patch("polyecho.discovery.worker.session_scope") as ms:
                ms.side_effect = lambda *x, **y: upsert_cand_writes.__setitem__(
                    "n", upsert_cand_writes["n"] + 1
                )
                return await real_cand(*a, **k)

        async def _run() -> None:
            with (
                patch.object(m, "get_datasource", return_value=FixtureDataSource()),
                patch.object(m, "_run_once", _run_once_capture),
                patch.object(w, "_upsert_thin", _thin_spy),
                patch.object(w, "_upsert_candidate", _cand_spy),
            ):
                await m.main(once=True)

        try:
            with patch.dict(
                os.environ,
                {"POLYECHO_DATASOURCE": "fixture"},
                clear=False,
            ):
                get_settings.cache_clear()
                asyncio.get_event_loop().run_until_complete(_run())
        finally:
            get_settings.cache_clear()
            m.set_persistence_enabled(True)

        # The deployed pass observed a CLOSED gate …
        assert gate_during_pass.get("enabled") is False
        # … and NO candidate write (thin OR full) ever opened its
        # session_scope body — 防再種 holds end-to-end even though the
        # fixture leaderboard WAS ranked (the read path is intact).
        assert upsert_thin_writes["n"] == 0
        assert upsert_cand_writes["n"] == 0

    # NOTE: ``test_main_fixture_with_optin_keeps_persistence`` was removed
    # — the ``POLYECHO_DISCOVERY_PERSIST_FIXTURE`` opt-in it asserted no
    # longer exists (不再有任何產生假資料的邏輯: fixture data can NEVER be
    # persisted, with no bypass).  The unconditional gate-closed behaviour
    # is covered by the gate-closed test above; the live-datasource path
    # (gate stays open) is covered by the test below.

    def test_main_live_like_datasource_keeps_persistence(self) -> None:
        """A non-FixtureDataSource (live / live-like) must NEVER trip the
        fixture gate — persistence stays enabled (gates on the resolved
        datasource object, not just the settings string)."""
        from polyecho.discovery import __main__ as m
        from polyecho.discovery.worker import persistence_enabled

        seen: dict[str, bool] = {}

        class _NotFixture:
            async def __aenter__(self) -> _NotFixture:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        async def _run_once_spy(worker: Any) -> None:
            seen["enabled"] = persistence_enabled()

        async def _run() -> None:
            with (
                patch.object(m, "get_datasource", return_value=_NotFixture()),
                patch.object(m, "_run_once", _run_once_spy),
            ):
                await m.main(once=True)

        try:
            asyncio.get_event_loop().run_until_complete(_run())
        finally:
            m.set_persistence_enabled(True)

        assert seen.get("enabled") is True


# ---------------------------------------------------------------------------
# §N1 — P0: _markets.fetch_hot_markets_gamma (live hot-market source)
#
# The frozen LiveDataSource.get_markets_by_volume hits the data-api
# /markets endpoint which is a hard 404; the volume-ranked listing lives
# on the gamma-api (BARE JSON list, different params).  These tests pin
# the new discovery-owned gamma source: descending early-stop,
# offset pagination, and the fail-soft (never-raise) contract.
# ---------------------------------------------------------------------------


class _FakeGammaResponse:
    """Minimal httpx.Response stand-in for the gamma /markets endpoint."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"{self._status}",
                request=httpx.Request("GET", "https://gamma/markets"),
                response=httpx.Response(self._status),
            )

    def json(self) -> Any:
        return self._payload


class _FakeGammaClient:
    """Async-context httpx.AsyncClient stand-in.

    ``pages`` is a list of payloads returned in offset order; each
    ``get`` call records its params and pops the next page.
    """

    def __init__(self, pages: list[Any]) -> None:
        self._pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeGammaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, path: str, params: dict[str, Any]) -> Any:
        self.calls.append({"path": path, "params": dict(params)})
        if not self._pages:
            return _FakeGammaResponse([])
        return _FakeGammaResponse(self._pages.pop(0))


def _gamma_row(condition_id: str, vol: float) -> dict[str, Any]:
    return {"conditionId": condition_id, "volume24hr": vol, "x": "ignored"}


class TestFetchHotMarketsGamma:
    """P0 — discovery-owned gamma-api hot-market source."""

    def test_descending_early_stop(self) -> None:
        """First sub-threshold row stops the scan (list is volume-desc)."""
        from polyecho.discovery import _markets

        # 5 rows desc; threshold 100k → only the first two qualify, the
        # third (90k) must trigger an immediate early-return (rows 4/5
        # are never inspected even though present).
        page = [
            _gamma_row("0xa", 500_000.0),
            _gamma_row("0xb", 120_000.0),
            _gamma_row("0xc", 90_000.0),
            _gamma_row("0xd", 80_000.0),
            _gamma_row("0xe", 1_000_000.0),  # out-of-order trap; unreached
        ]
        fake = _FakeGammaClient([page])

        async def _run() -> list[tuple[str, dict[str, Any]]]:
            with patch.object(
                _markets,
                "make_async_client_resolved",
                AsyncMock(return_value=fake),
            ):
                return await _markets.fetch_hot_markets_gamma(min_vol=100_000.0, limit=200)

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert [cid for cid, _ in out] == ["0xa", "0xb"]
        # Stopped after page 1 — no second page request.
        assert len(fake.calls) == 1
        # Volume-desc params forwarded as the blueprint specifies.
        p = fake.calls[0]["params"]
        assert p["order"] == "volume24hr"
        assert p["ascending"] == "false"
        assert p["active"] == "true"
        assert p["closed"] == "false"
        assert p["offset"] == 0

    def test_pagination_advances_offset_until_short_page(self) -> None:
        """Full pages advance ``offset``; a short page ends pagination."""
        from polyecho.discovery import _markets

        full = [_gamma_row(f"0x{i:02x}", 200_000.0) for i in range(100)]
        tail = [_gamma_row("0xff", 150_000.0)]  # short page → stop
        fake = _FakeGammaClient([full, tail])

        async def _run() -> list[tuple[str, dict[str, Any]]]:
            with patch.object(
                _markets,
                "make_async_client_resolved",
                AsyncMock(return_value=fake),
            ):
                return await _markets.fetch_hot_markets_gamma(min_vol=100_000.0, limit=500)

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert len(out) == 101
        assert len(fake.calls) == 2
        assert fake.calls[0]["params"]["offset"] == 0
        assert fake.calls[1]["params"]["offset"] == 100

    def test_limit_truncates_results(self) -> None:
        from polyecho.discovery import _markets

        page = [_gamma_row(f"0x{i:02x}", 300_000.0) for i in range(50)]
        fake = _FakeGammaClient([page])

        async def _run() -> list[tuple[str, dict[str, Any]]]:
            with patch.object(
                _markets,
                "make_async_client_resolved",
                AsyncMock(return_value=fake),
            ):
                return await _markets.fetch_hot_markets_gamma(min_vol=0.0, limit=10)

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert len(out) == 10

    def test_fail_soft_on_http_error_returns_empty(self) -> None:
        """A transport/HTTP error degrades to [] — never raises."""
        from polyecho.discovery import _markets

        class _BoomClient(_FakeGammaClient):
            async def get(self, path: str, params: dict[str, Any]) -> Any:
                import httpx

                raise httpx.ConnectError("gamma unreachable")

        fake = _BoomClient([])

        async def _run() -> list[tuple[str, dict[str, Any]]]:
            with patch.object(
                _markets,
                "make_async_client_resolved",
                AsyncMock(return_value=fake),
            ):
                return await _markets.fetch_hot_markets_gamma(min_vol=100_000.0, limit=200)

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert out == []

    def test_fail_soft_on_non_list_payload(self) -> None:
        """data-api-style {"data": [...]} envelope (not a bare list) is
        treated as exhausted, not a crash."""
        from polyecho.discovery import _markets

        fake = _FakeGammaClient([{"data": [_gamma_row("0xa", 9e9)]}])

        async def _run() -> list[tuple[str, dict[str, Any]]]:
            with patch.object(
                _markets,
                "make_async_client_resolved",
                AsyncMock(return_value=fake),
            ):
                return await _markets.fetch_hot_markets_gamma(min_vol=100_000.0, limit=200)

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert out == []

    def test_non_positive_limit_returns_empty_without_network(self) -> None:
        from polyecho.discovery import _markets

        called = {"n": 0}

        async def _factory(**_kw: Any) -> Any:
            called["n"] += 1
            return _FakeGammaClient([])

        async def _run() -> list[tuple[str, dict[str, Any]]]:
            with patch.object(_markets, "make_async_client_resolved", _factory):
                return await _markets.fetch_hot_markets_gamma(min_vol=0.0, limit=0)

        out = asyncio.get_event_loop().run_until_complete(_run())
        assert out == []
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# §N2 — P0: discover_hot_market_holders datasource split
#
# Fixture path MUST stay byte-identical (calls get_markets_by_volume on
# the FixtureDataSource); the live path MUST source via the gamma helper
# and degrade fail-soft to an empty pass + one
# ``discovery_hot_markets_unavailable`` warning.
# ---------------------------------------------------------------------------


class _HotMarketFixtureSpyDS(FixtureDataSource):
    """Real FixtureDataSource that records the get_markets_by_volume call
    so we can prove the fixture path is unchanged (still uses it)."""

    def __init__(self) -> None:
        super().__init__()
        self.gmbv_calls: list[dict[str, Any]] = []

    async def get_markets_by_volume(
        self, *, min_volume_24h: float = 100_000.0, limit: int = 200
    ) -> list[Any]:
        self.gmbv_calls.append({"min_volume_24h": min_volume_24h, "limit": limit})
        return await super().get_markets_by_volume(min_volume_24h=min_volume_24h, limit=limit)


class TestDiscoverHotMarketHoldersDatasourceSplit:
    def test_fixture_path_unchanged_uses_get_markets_by_volume(self) -> None:
        """Fixture mode keeps calling the frozen FixtureDataSource method
        verbatim — gamma is NOT invoked on the fixture path."""
        from polyecho.discovery import worker as wmod

        ds = _HotMarketFixtureSpyDS()
        processed: list[tuple[str, str]] = []
        gamma_called = {"n": 0}

        async def _fake_gamma(**_kw: Any) -> list[Any]:
            gamma_called["n"] += 1
            return []

        async def _run() -> None:
            worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append((w, s))

            with (
                patch.object(wmod, "fetch_hot_markets_gamma", _fake_gamma),
                patch.object(worker, "_process_wallet", _fake_process),
            ):
                await worker.discover_hot_market_holders()

        asyncio.get_event_loop().run_until_complete(_run())
        # Fixture path used the frozen method with the unchanged args …
        assert ds.gmbv_calls == [{"min_volume_24h": 100_000.0, "limit": 200}]
        # … and the live gamma source was NOT touched.
        assert gamma_called["n"] == 0
        # All surfaced wallets are tagged with the hot_market_holders src.
        assert processed
        assert all(s == "hot_market_holders" for _w, s in processed)

    def test_live_path_uses_gamma_then_top_holders(self) -> None:
        """Non-fixture datasource → markets come from the gamma helper,
        and their condition ids feed get_market_top_holders."""
        from polyecho.discovery import worker as wmod

        seen_cids: list[str] = []

        class _LiveLikeDS:
            async def get_markets_by_volume(self, **_kw: Any) -> list[Any]:
                raise AssertionError("live path must NOT call the frozen (404) method")

            async def get_market_top_holders(
                self, condition_id: str, *, limit: int = 10
            ) -> list[PositionRecord]:
                seen_cids.append(condition_id)
                return [
                    _make_position(
                        wallet=f"0xholder_{condition_id[-1]}",
                    )
                ]

        async def _fake_gamma(*, min_vol: float, limit: int) -> list[tuple[str, dict[str, Any]]]:
            assert min_vol == 100_000.0
            return [("0xcond_a", {}), ("0xcond_b", {})]

        processed: list[tuple[str, str]] = []

        async def _run() -> None:
            worker = DiscoveryWorker(
                _LiveLikeDS(),  # type: ignore[arg-type]
                follower_capital_usd=1000.0,
            )

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append((w, s))

            with (
                patch.object(wmod, "fetch_hot_markets_gamma", _fake_gamma),
                patch.object(worker, "_process_wallet", _fake_process),
            ):
                await worker.discover_hot_market_holders()

        asyncio.get_event_loop().run_until_complete(_run())
        assert seen_cids == ["0xcond_a", "0xcond_b"]
        assert sorted(w for w, _ in processed) == [
            "0xholder_a",
            "0xholder_b",
        ]

    def test_live_path_gamma_empty_degrades_failsoft_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Permanent gamma failure → empty pass + ONE clear
        ``discovery_hot_markets_unavailable`` warning, no retry storm,
        get_market_top_holders never called."""
        from polyecho.discovery import worker as wmod

        class _LiveLikeDS:
            async def get_market_top_holders(
                self, condition_id: str, *, limit: int = 10
            ) -> list[PositionRecord]:
                raise AssertionError("must not be reached when no markets resolved")

        async def _empty_gamma(*, min_vol: float, limit: int) -> list[tuple[str, dict[str, Any]]]:
            return []

        processed: list[tuple[str, str]] = []

        async def _run() -> None:
            worker = DiscoveryWorker(
                _LiveLikeDS(),  # type: ignore[arg-type]
                follower_capital_usd=1000.0,
            )

            async def _fake_process(w: str, s: str, **kw: Any) -> None:
                processed.append((w, s))

            with (
                patch.object(wmod, "fetch_hot_markets_gamma", _empty_gamma),
                patch.object(worker, "_process_wallet", _fake_process),
            ):
                with caplog.at_level("WARNING"):
                    await worker.discover_hot_market_holders()

        asyncio.get_event_loop().run_until_complete(_run())
        assert processed == []
        assert "discovery_hot_markets_unavailable" in caplog.text


# ---------------------------------------------------------------------------
# §N3 — P1: PnL sigmoid scale fidelity fix (regression pins)
#
# The scale was raised 10K → 250K (``_PNL_SIGMOID_SCALE_USD``) because at
# 10K the dominant 25%-weight PnL sub-metric saturated to ~100 for any
# pnl ≳ $50K across the real top-200.  These tests pin representative
# points so the scale cannot silently regress.
# ---------------------------------------------------------------------------


class TestPnlSigmoidScaleFidelity:
    @staticmethod
    def _pnl_only(pnl: float) -> dict[str, Any]:
        # Isolate the PnL term: all other sub-metrics zeroed so the score
        # is exactly ``100 * sigmoid(pnl/scale) * 0.25``.
        return {
            "pnl_usd": pnl,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 1.0,
            "consistency": 0.0,
            "specialization": 0.0,
        }

    def test_scale_constant_is_250k(self) -> None:
        from polyecho.discovery.scoring import _PNL_SIGMOID_SCALE_USD

        assert _PNL_SIGMOID_SCALE_USD == 250_000.0

    def test_representative_points_pinned(self) -> None:
        # pnl_norm = 100*sigmoid(pnl/250k); profit_score weight = 0.25.
        # $50K → 13.7458, $250K → 18.2765, $1M → 24.5503  (4dp, the
        # documented discrimination band the 10K scale had flattened).
        assert profit_score(self._pnl_only(50_000.0)) == pytest.approx(13.7458, abs=1e-3)
        assert profit_score(self._pnl_only(250_000.0)) == pytest.approx(18.2765, abs=1e-3)
        assert profit_score(self._pnl_only(1_000_000.0)) == pytest.approx(24.5503, abs=1e-3)

    def test_discriminates_across_real_top200_band(self) -> None:
        """The whole point of the fix: $250K and $1M must NOT collapse to
        the same score (they did at the old 10K scale, both ~25.0)."""
        s_50k = profit_score(self._pnl_only(50_000.0))
        s_250k = profit_score(self._pnl_only(250_000.0))
        s_1m = profit_score(self._pnl_only(1_000_000.0))
        s_35m = profit_score(self._pnl_only(3_500_000.0))
        # Strictly increasing with a meaningful (> 1 pt) spread between
        # the mid-distribution points the old scale had pinned at ~25.
        assert s_50k < s_250k < s_1m < s_35m
        assert s_1m - s_250k > 1.0
        assert s_250k - s_50k > 1.0

    def test_zero_pnl_still_midpoint(self) -> None:
        # Shape preserved: sigmoid(0)=0.5 → 12.5 with 0.25 weight.
        assert profit_score(self._pnl_only(0.0)) == pytest.approx(12.5, abs=1e-6)

    def test_extremes_still_saturate(self) -> None:
        # Numerical-stability contract is unchanged by the rescale.
        assert profit_score(self._pnl_only(1e12)) == pytest.approx(25.0, abs=1e-3)
        assert profit_score(self._pnl_only(-1e12)) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# §N4 — P2: verified-first detail-budget ordering
#
# When the Phase-2 detail budget saturates, the surviving subset must be
# the *verified* leaders (then ascending wallet hex), not whoever has the
# lowest wallet hex.  Still a total + stable + deterministic key.
# ---------------------------------------------------------------------------


class TestVerifiedFirstBudgetOrdering:
    def test_verified_entries_win_the_detail_budget(self) -> None:
        from polyecho.discovery import worker as wmod
        from polyecho.shared.datasource import LeaderboardEntry

        # 3 entries: an UNVERIFIED low-hex wallet (would win a pure-hex
        # sort) and two VERIFIED higher-hex wallets.  With budget 2 the
        # verified pair must be selected; the low-hex unverified loses.
        entries = [
            LeaderboardEntry(
                rank=1,
                wallet="0x0000000000000000000000000000000000000001",
                username="lowhex_unverified",
                pnl=1.0,
                volume=1.0,
                verified=False,
            ),
            LeaderboardEntry(
                rank=2,
                wallet="0x00000000000000000000000000000000000000ff",
                username="hi_verified_b",
                pnl=1.0,
                volume=1.0,
                verified=True,
            ),
            LeaderboardEntry(
                rank=3,
                wallet="0x00000000000000000000000000000000000000aa",
                username="hi_verified_a",
                pnl=1.0,
                volume=1.0,
                verified=True,
            ),
        ]

        class _LBStub:
            async def get_leaderboard(self, **_kw: Any) -> list[Any]:
                return list(entries)

        full_called: list[str] = []

        async def _run() -> None:
            worker = DiscoveryWorker(
                _LBStub(),  # type: ignore[arg-type]
                follower_capital_usd=1000.0,
            )

            # Thin upsert: pretend every entry is NEW (forces all into
            # _needs_full_detail so only the budget slice decides).
            async def _thin(*_a: Any, **_k: Any) -> tuple[Any, ...]:
                return (None, None, None, {})

            async def _fake_full(w: str, s: str, **_k: Any) -> None:
                full_called.append(w)

            with (
                patch.object(wmod, "_upsert_thin", _thin),
                patch.object(wmod, "_DISCOVERY_DETAIL_BUDGET", 2),
                patch.object(worker, "_process_wallet", _fake_full),
            ):
                await worker.discover_leaderboard()

        asyncio.get_event_loop().run_until_complete(_run())
        # The two VERIFIED wallets are the budgeted subset (verified
        # first); the low-hex UNVERIFIED wallet is dropped by the budget.
        assert sorted(full_called) == [
            "0x00000000000000000000000000000000000000aa",
            "0x00000000000000000000000000000000000000ff",
        ]
        assert "0x0000000000000000000000000000000000000001" not in full_called


# ---------------------------------------------------------------------------
# §N5 — P2: per-source heartbeat counters
#
# The F0 heartbeat ``counters`` sub-dict must surface a per-source scan
# count so a dead source (e.g. live hot-markets 404) shows
# ``hot_market_holders: 0`` instead of the worker merely looking healthy
# off the other sources.
# ---------------------------------------------------------------------------


class TestPerSourceHeartbeatCounters:
    def test_counters_seeded_with_all_source_keys(self) -> None:
        worker = DiscoveryWorker(FixtureDataSource(), follower_capital_usd=1000.0)
        for key in (
            "hot_market_holders",
            "leaderboard",
            "large_new_positions",
        ):
            assert worker._pass_counters[key] == 0

    def test_process_wallet_increments_its_source_counter(self) -> None:
        # ``scanned`` and the per-source counter are bumped at the TOP of
        # ``_process_wallet``, before the (gathered) datasource fetch.
        # Make the fetch fail so the method early-returns right after the
        # counter bump — deterministic, no DB/persistence involved.
        class _FetchFailsDS:
            async def get_user_activity(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("fetch deliberately failed for test")

            async def get_positions(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("fetch deliberately failed for test")

        worker = DiscoveryWorker(
            _FetchFailsDS(),  # type: ignore[arg-type]
            follower_capital_usd=1000.0,
        )

        async def _run() -> None:
            await worker._process_wallet(W_SWING, "hot_market_holders")
            await worker._process_wallet(W_SCALPER, "leaderboard")
            await worker._process_wallet(W_SWING, "hot_market_holders")

        asyncio.get_event_loop().run_until_complete(_run())
        assert worker._pass_counters["hot_market_holders"] == 2
        assert worker._pass_counters["leaderboard"] == 1
        assert worker._pass_counters["large_new_positions"] == 0
        # ``scanned`` still counts every attempt regardless of source.
        assert worker._pass_counters["scanned"] == 3

    def test_drain_resets_all_keys_including_sources(self) -> None:
        """A drained heartbeat must keep reporting every source key (a
        dead source = ``<source>: 0``, never a vanished key)."""
        worker = DiscoveryWorker(FixtureDataSource(), follower_capital_usd=1000.0)
        worker._pass_counters["hot_market_holders"] = 7
        worker._pass_counters["scanned"] = 9

        snap = worker.drain_pass_counters()
        assert snap["hot_market_holders"] == 7
        assert snap["scanned"] == 9
        # Reset baseline still carries ALL source keys at 0.
        for key in (
            "scanned",
            "scored",
            "emitted",
            "hot_market_holders",
            "leaderboard",
            "large_new_positions",
        ):
            assert worker._pass_counters[key] == 0
