"""Tests for paper-session summary recomputation (C1-P3)."""

from __future__ import annotations

from polyecho.paper.summary import PaperFill, recompute_summary


def _fill(**kw: object) -> PaperFill:
    base: dict[str, object] = {
        "side": "BUY",
        "pnl": 0.0,
        "balance_after": None,
        "condition_id": "0xmarket1",
        "market_known": True,
    }
    base.update(kw)
    return PaperFill(**base)  # type: ignore[arg-type]


def test_trade_count_is_number_of_executed_fills() -> None:
    fills = [_fill(side="BUY"), _fill(side="SELL")]
    summary = recompute_summary(fills, capital_usd=1000.0, cycle_observed=2, cycle_executed=2)
    assert summary.trade_count == 2


def test_copyable_pnl_is_sum_of_fill_pnl() -> None:
    fills = [_fill(pnl=10.0), _fill(pnl=-3.0)]
    summary = recompute_summary(fills, capital_usd=1000.0, cycle_observed=2, cycle_executed=2)
    assert summary.copyable_pnl == 7.0


def test_max_drawdown_is_negative_fraction_of_peak() -> None:
    # Curve: 1000 -> 1100 -> 880. Peak 1100, trough 880 => -20%.
    fills = [
        _fill(pnl=100.0, balance_after=1100.0),
        _fill(pnl=-220.0, balance_after=880.0),
    ]
    summary = recompute_summary(fills, capital_usd=1000.0, cycle_observed=2, cycle_executed=2)
    assert summary.max_drawdown == (880.0 - 1100.0) / 1100.0


def test_max_drawdown_zero_when_curve_only_rises() -> None:
    fills = [
        _fill(pnl=50.0, balance_after=1050.0),
        _fill(pnl=50.0, balance_after=1100.0),
    ]
    summary = recompute_summary(fills, capital_usd=1000.0, cycle_observed=2, cycle_executed=2)
    assert summary.max_drawdown == 0.0


def test_drawdown_rebuilds_curve_when_balance_after_missing() -> None:
    # No balance_after: the curve is rebuilt from capital + running PnL.
    fills = [_fill(pnl=100.0), _fill(pnl=-250.0)]
    summary = recompute_summary(fills, capital_usd=1000.0, cycle_observed=2, cycle_executed=2)
    # 1000 -> 1100 -> 850. Drawdown = (850-1100)/1100.
    assert summary.max_drawdown == (850.0 - 1100.0) / 1100.0


def test_unknown_exposure_ratio() -> None:
    fills = [
        _fill(market_known=True),
        _fill(market_known=False),
        _fill(market_known=False),
        _fill(market_known=True),
    ]
    summary = recompute_summary(fills, capital_usd=1000.0, cycle_observed=4, cycle_executed=4)
    assert summary.unknown_exposure_ratio == 0.5


def test_filtered_ratio_is_filtered_over_cycle_observed() -> None:
    # This cycle observed 4 leader trades, executed 2 => 2 filtered.
    fills = [_fill(), _fill()]
    summary = recompute_summary(fills, capital_usd=1000.0, cycle_observed=4, cycle_executed=2)
    assert summary.filtered_ratio == 0.5


def test_filtered_ratio_zero_when_nothing_observed() -> None:
    summary = recompute_summary([], capital_usd=1000.0, cycle_observed=0, cycle_executed=0)
    assert summary.filtered_ratio == 0.0


def test_empty_fills_yield_zeroed_summary() -> None:
    summary = recompute_summary([], capital_usd=1000.0, cycle_observed=0, cycle_executed=0)
    assert summary.trade_count == 0
    assert summary.copyable_pnl == 0.0
    assert summary.max_drawdown == 0.0
    assert summary.unknown_exposure_ratio == 0.0
    assert summary.filtered_ratio == 0.0
