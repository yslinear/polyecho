"""Tests for the PolyHermes paper-trade copyability scorer (C1).

Every one of the eleven additive components is exercised at a
representative input, plus the two caps (no-session <= 20, thin-sample
<= 59) and a high-copyability session that clears the 60 promote gate.
The scorer is pure, so all of this runs with no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polyecho.paper.copyability import (
    MIN_SAMPLE_TRADES,
    SAMPLE_INSUFFICIENT_CAP,
    SCORE_VERSION,
    CopyabilityResult,
    PaperSessionStats,
    score_copyability,
)

NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _stats(**kw: object) -> PaperSessionStats:
    """A paper session with high-copyability defaults (every component maxed)."""
    base: dict[str, object] = {
        "copyable_pnl": 50.0,
        "trade_count": 20,
        "unknown_exposure_ratio": 0.0,
        "filtered_ratio": 0.0,
        "max_drawdown": -0.02,
        "started_at": NOW - timedelta(days=10),
    }
    base.update(kw)
    return PaperSessionStats(**base)  # type: ignore[arg-type]


def _score(stats: PaperSessionStats | None, **kw: object) -> CopyabilityResult:
    return score_copyability(stats, now=NOW, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Component 1 -- profit_signal (max 20)
# ---------------------------------------------------------------------------


class TestProfitSignal:
    def test_pnl_over_10_scores_full_20(self) -> None:
        assert _score(_stats(copyable_pnl=11.0)).components["profit_signal"] == 20.0

    def test_pnl_positive_under_10_is_linear(self) -> None:
        # copyable_pnl 4 -> clamp(4 * 2, 0, 20) == 8.
        assert _score(_stats(copyable_pnl=4.0)).components["profit_signal"] == 8.0

    def test_pnl_zero_or_loss_scores_zero(self) -> None:
        assert _score(_stats(copyable_pnl=0.0)).components["profit_signal"] == 0.0
        assert _score(_stats(copyable_pnl=-20.0)).components["profit_signal"] == 0.0


# ---------------------------------------------------------------------------
# Component 2 -- repeatability (max 15)
# ---------------------------------------------------------------------------


class TestRepeatability:
    def test_trade_count_is_linear_then_clamped(self) -> None:
        # 6 trades -> 6 * 1.5 == 9.
        assert _score(_stats(trade_count=6)).components["repeatability"] == 9.0

    def test_repeatability_clamps_at_15(self) -> None:
        # 100 trades -> clamp(150, 0, 15) == 15.
        assert _score(_stats(trade_count=100)).components["repeatability"] == 15.0


# ---------------------------------------------------------------------------
# Component 3 -- liquidity_fit (max 10)
# ---------------------------------------------------------------------------


class TestLiquidityFit:
    def test_no_unknown_exposure_scores_full_10(self) -> None:
        assert _score(_stats(unknown_exposure_ratio=0.0)).components["liquidity_fit"] == 10.0

    def test_partial_unknown_exposure_is_linear(self) -> None:
        # 0.30 unknown -> 10 - 0.30 * 10 == 7.
        assert _score(_stats(unknown_exposure_ratio=0.30)).components["liquidity_fit"] == 7.0


# ---------------------------------------------------------------------------
# Component 4 -- entry_price_fit (max 10)
# ---------------------------------------------------------------------------


class TestEntryPriceFit:
    def test_no_filtering_scores_full_10(self) -> None:
        assert _score(_stats(filtered_ratio=0.0)).components["entry_price_fit"] == 10.0

    def test_partial_filtering_is_linear(self) -> None:
        # 0.40 filtered -> 10 - 0.40 * 10 == 6.
        assert _score(_stats(filtered_ratio=0.40)).components["entry_price_fit"] == 6.0


# ---------------------------------------------------------------------------
# Component 5 -- slippage_risk (max 10, step function)
# ---------------------------------------------------------------------------


class TestSlippageRisk:
    def test_low_unknown_exposure_scores_10(self) -> None:
        assert _score(_stats(unknown_exposure_ratio=0.20)).components["slippage_risk"] == 10.0

    def test_high_unknown_exposure_scores_4(self) -> None:
        assert _score(_stats(unknown_exposure_ratio=0.21)).components["slippage_risk"] == 4.0


# ---------------------------------------------------------------------------
# Component 6 -- holding_period_fit (max 5)
# ---------------------------------------------------------------------------


class TestHoldingPeriodFit:
    def test_session_at_least_7d_scores_full_5(self) -> None:
        cand = _stats(started_at=NOW - timedelta(days=7))
        assert _score(cand).components["holding_period_fit"] == 5.0

    def test_younger_session_is_linear(self) -> None:
        # A 3.5-day-old session -> (3.5 / 7) * 5 == 2.5.
        cand = _stats(started_at=NOW - timedelta(days=3, hours=12))
        assert _score(cand).components["holding_period_fit"] == 2.5


# ---------------------------------------------------------------------------
# Component 7 -- market_type_risk (constant 5)
# ---------------------------------------------------------------------------


class TestMarketTypeRisk:
    def test_market_type_risk_is_constant_5(self) -> None:
        assert _score(_stats()).components["market_type_risk"] == 5.0
        # Constant even with no session.
        assert _score(None).components["market_type_risk"] == 5.0


# ---------------------------------------------------------------------------
# Component 8 -- drawdown_risk (max 10, bucketed; fraction units)
# ---------------------------------------------------------------------------


class TestDrawdownRisk:
    def test_none_drawdown_scores_neutral_5(self) -> None:
        assert _score(_stats(max_drawdown=None)).components["drawdown_risk"] == 5.0

    def test_shallow_drawdown_scores_10(self) -> None:
        # -0.05 (a 5% drawdown) is the top bucket.
        assert _score(_stats(max_drawdown=-0.05)).components["drawdown_risk"] == 10.0

    def test_moderate_drawdown_scores_7(self) -> None:
        assert _score(_stats(max_drawdown=-0.15)).components["drawdown_risk"] == 7.0

    def test_deep_drawdown_scores_3(self) -> None:
        assert _score(_stats(max_drawdown=-0.20)).components["drawdown_risk"] == 3.0

    def test_severe_drawdown_scores_0(self) -> None:
        assert _score(_stats(max_drawdown=-0.25)).components["drawdown_risk"] == 0.0


# ---------------------------------------------------------------------------
# Component 9 -- exit_liquidity_risk (max 5, step function)
# ---------------------------------------------------------------------------


class TestExitLiquidityRisk:
    def test_low_unknown_exposure_scores_5(self) -> None:
        assert _score(_stats(unknown_exposure_ratio=0.20)).components["exit_liquidity_risk"] == 5.0

    def test_high_unknown_exposure_scores_1(self) -> None:
        assert _score(_stats(unknown_exposure_ratio=0.50)).components["exit_liquidity_risk"] == 1.0


# ---------------------------------------------------------------------------
# Component 10 -- data_freshness (max 5)
# ---------------------------------------------------------------------------


class TestDataFreshness:
    def test_source_seen_within_48h_scores_5(self) -> None:
        result = _score(_stats(), source_last_seen=NOW - timedelta(hours=47))
        assert result.components["data_freshness"] == 5.0

    def test_stale_source_scores_0(self) -> None:
        result = _score(_stats(), source_last_seen=NOW - timedelta(hours=49))
        assert result.components["data_freshness"] == 0.0

    def test_never_seen_source_scores_0(self) -> None:
        assert _score(_stats(), source_last_seen=None).components["data_freshness"] == 0.0


# ---------------------------------------------------------------------------
# Component 11 -- filter_pass_rate (max 5)
# ---------------------------------------------------------------------------


class TestFilterPassRate:
    def test_no_filtering_scores_full_5(self) -> None:
        assert _score(_stats(filtered_ratio=0.0)).components["filter_pass_rate"] == 5.0

    def test_partial_filtering_is_linear(self) -> None:
        # 0.40 filtered -> 5 - 0.40 * 5 == 3.
        assert _score(_stats(filtered_ratio=0.40)).components["filter_pass_rate"] == 3.0


# ---------------------------------------------------------------------------
# No-session score caps at 20
# ---------------------------------------------------------------------------


class TestNoSessionScore:
    def test_no_session_scores_at_most_20(self) -> None:
        # No session + fresh source: market_type 5 + drawdown 5 +
        # slippage 4 + exit_liquidity 1 + data_freshness 5 == 20.
        result = _score(None, source_last_seen=NOW - timedelta(hours=1))
        assert result.total == 20.0
        assert result.raw_total == 20.0

    def test_no_session_never_exceeds_20(self) -> None:
        # Even a stale source only lowers it (data_freshness 0 -> 15).
        result = _score(None, source_last_seen=None)
        assert result.total <= 20.0
        assert result.total == 15.0

    def test_no_session_paper_components_take_no_data_values(self) -> None:
        c = _score(None).components
        assert c["profit_signal"] == 0.0
        assert c["repeatability"] == 0.0
        assert c["liquidity_fit"] == 0.0
        assert c["entry_price_fit"] == 0.0
        assert c["slippage_risk"] == 4.0
        assert c["holding_period_fit"] == 0.0
        assert c["drawdown_risk"] == 5.0
        assert c["exit_liquidity_risk"] == 1.0
        assert c["filter_pass_rate"] == 0.0


# ---------------------------------------------------------------------------
# Sample-size cap (< 10 trades -> total held at 59)
# ---------------------------------------------------------------------------


class TestSampleSizeCap:
    def test_thin_sample_high_score_is_capped_at_59(self) -> None:
        # A high-copyability session but only 9 trades: raw total clears 59
        # yet the thin-sample cap holds the final total at 59.
        result = _score(_stats(trade_count=MIN_SAMPLE_TRADES - 1))
        assert result.raw_total > SAMPLE_INSUFFICIENT_CAP
        assert result.total == float(SAMPLE_INSUFFICIENT_CAP)
        assert result.sample_capped is True

    def test_full_sample_high_score_is_not_capped(self) -> None:
        # The same session with >= 10 trades keeps its raw total.
        result = _score(_stats(trade_count=MIN_SAMPLE_TRADES))
        assert result.total == result.raw_total
        assert result.sample_capped is False

    def test_thin_sample_low_score_is_not_capped(self) -> None:
        # A thin sample whose raw total is below 59 is left alone (a poor
        # session: a loss, mostly-unknown markets, a deep drawdown).
        result = _score(
            _stats(
                trade_count=2,
                copyable_pnl=-5.0,
                unknown_exposure_ratio=0.90,
                filtered_ratio=0.80,
                max_drawdown=-0.30,
            )
        )
        assert result.raw_total <= SAMPLE_INSUFFICIENT_CAP
        assert result.total == result.raw_total
        assert result.sample_capped is False


# ---------------------------------------------------------------------------
# High-copyability session clears the 60 promote gate
# ---------------------------------------------------------------------------


class TestHighCopyabilityScore:
    def test_strong_session_scores_at_least_60(self) -> None:
        # Every component maxed + a fresh source: well above 60.
        result = _score(_stats(), source_last_seen=NOW - timedelta(hours=1))
        assert result.total >= 60.0

    def test_total_never_exceeds_100(self) -> None:
        result = _score(_stats(), source_last_seen=NOW - timedelta(hours=1))
        assert result.total <= 100.0
        assert result.raw_total <= 100.0


# ---------------------------------------------------------------------------
# Result metadata
# ---------------------------------------------------------------------------


def test_result_stamps_the_score_version() -> None:
    assert _score(_stats()).score_version == SCORE_VERSION
    assert SCORE_VERSION == "research-copyability-v1"


def test_score_defaults_now_to_utc_when_omitted() -> None:
    # score_copyability() must not crash when `now` is not injected.
    result = score_copyability(PaperSessionStats(trade_count=1))
    assert result.score_version == SCORE_VERSION
