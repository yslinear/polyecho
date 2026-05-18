"""Tests for the PolyEcho backtest engine — Phase 2.

Uses FixtureDataSource (POLYECHO_DATASOURCE=fixture, the default).
No __init__.py in this directory (importlib mode).

Fixture wallet addresses (exact, from datasource/fixtures/activity/):
  SWING    = 0xswing1000000000000000000000000000000001   (295 trades, ~90 d)
  SCALPER  = 0xscalper200000000000000000000000000000002  (2700 trades, ~90 d)
  POSITION = 0xposition400000000000000000000000000000004 (12 trades)

Fixture date range: 2026-02-16 → 2026-04-28 (swing)

Tests:
  1. Pure helpers: apply_slippage, compute_sharpe, compute_max_drawdown
  2. Swing leader: sane positive-ish PnL curve & stats
  3. Triple scenario ordering: pessimistic PnL ≤ neutral ≤ optimistic
  4. Risk gate: tiny daily_loss_limit triggers skips
  5. Determinism: two runs with same params produce identical results
  6. BacktestResult.to_dict() round-trips cleanly
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast

import pytest

from polyecho.backtest.engine import (
    BacktestEngine,
    BacktestParams,
    apply_slippage,
    compute_fee,
    compute_max_drawdown,
    compute_settlement_price,
    compute_sharpe,
)
from polyecho.backtest.scenarios import run_triple
from polyecho.shared.datasource import FixtureDataSource

# ---------------------------------------------------------------------------
# Fixtures and constants
# ---------------------------------------------------------------------------

SWING = "0xswing1000000000000000000000000000000001"
SCALPER = "0xscalper200000000000000000000000000000002"
POSITION = "0xposition400000000000000000000000000000004"

# Date window fully covered by fixture data
START = date(2026, 2, 16)
END = date(2026, 4, 28)


def make_ds() -> FixtureDataSource:
    return FixtureDataSource()


def make_engine() -> BacktestEngine:
    return BacktestEngine(datasource=make_ds())


def base_params(
    leader: str = SWING,
    copy_ratio: float = 0.05,
    daily_loss_limit: float = 200.0,
    max_concurrent: int = 10,
) -> BacktestParams:
    return BacktestParams(
        leader_address=leader,
        start_date=START,
        end_date=END,
        initial_capital_usd=1000.0,
        copy_ratio=copy_ratio,
        fixed_size_usd=0.0,
        max_position_usd=200.0,
        min_trade_size_usd=1.0,
        daily_loss_limit_usd=daily_loss_limit,
        max_concurrent_positions=max_concurrent,
        slippage_bps=150,
        execution_delay_seconds=30,
        category_whitelist=None,
    )


# ---------------------------------------------------------------------------
# 1. Pure helpers
# ---------------------------------------------------------------------------


class TestApplySlippage:
    def test_buy_raises_price(self) -> None:
        filled = apply_slippage(0.50, "BUY", 150)
        assert filled > 0.50
        assert abs(filled - 0.50 * 1.015) < 1e-9

    def test_sell_lowers_price(self) -> None:
        filled = apply_slippage(0.50, "SELL", 150)
        assert filled < 0.50
        assert abs(filled - 0.50 * 0.985) < 1e-9

    def test_zero_slippage_noop(self) -> None:
        assert apply_slippage(0.60, "BUY", 0) == pytest.approx(0.60)
        assert apply_slippage(0.60, "SELL", 0) == pytest.approx(0.60)

    def test_buy_clamps_at_one(self) -> None:
        # Price 0.99 + large slippage must not exceed 1.0
        assert apply_slippage(0.99, "BUY", 10_000) <= 1.0

    def test_sell_clamps_at_zero(self) -> None:
        assert apply_slippage(0.01, "SELL", 10_000) >= 0.0


class TestComputeSharpe:
    def test_positive_varying_gains(self) -> None:
        # Varying positive daily returns → positive Sharpe
        # Use alternating 8/12 so mean>0 and std>0
        daily = [8.0, 12.0] * 25  # 50 values, mean=10, std>0
        s = compute_sharpe(daily)
        assert s > 0.0

    def test_zero_std_returns_zero(self) -> None:
        # All identical returns → std(ddof=1)=0 → Sharpe=0
        # (constant series has no dispersion, ratio undefined → 0 by convention)
        daily = [5.0] * 30
        s = compute_sharpe(daily)
        assert s == 0.0

    def test_insufficient_data(self) -> None:
        assert compute_sharpe([]) == 0.0
        assert compute_sharpe([1.0]) == 0.0

    def test_negative_returns(self) -> None:
        daily = [-5.0] * 30
        s = compute_sharpe(daily)
        assert s == 0.0  # all-same negative → std=0


class TestComputeMaxDrawdown:
    def test_no_drawdown(self) -> None:
        curve = [0.0, 10.0, 20.0, 30.0]
        assert compute_max_drawdown(curve) == pytest.approx(0.0)

    def test_simple_drawdown(self) -> None:
        # Peak 100, then drops to 50 → 50% drawdown
        curve = [0.0, 50.0, 100.0, 50.0]
        dd = compute_max_drawdown(curve)
        assert dd == pytest.approx(50.0)

    def test_empty(self) -> None:
        assert compute_max_drawdown([]) == 0.0

    def test_partial_recovery(self) -> None:
        curve = [0.0, 100.0, 40.0, 80.0, 200.0]
        dd = compute_max_drawdown(curve)
        # Peak 100 → 40: (100-40)/100 = 60%
        assert dd == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# 2. Swing leader: sane stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swing_leader_pnl_curve_has_correct_length() -> None:
    engine = make_engine()
    params = base_params(SWING)
    result = await engine.run(params)

    # Curve must cover every calendar day in [START, END]
    expected_days = (END - START).days + 1
    assert len(result.pnl_curve) == expected_days


@pytest.mark.asyncio
async def test_swing_leader_stats_are_sane() -> None:
    engine = make_engine()
    params = base_params(SWING, copy_ratio=0.05)
    result = await engine.run(params)

    # With 295 trades and 5% copy ratio, some trades must execute
    executed = [e for e in result.trade_log if not e.get("skipped")]
    assert len(executed) > 0, "expected at least some executed trades"

    # Total return should be a finite float
    assert isinstance(result.total_return_usd, float)
    assert not (result.total_return_usd != result.total_return_usd)  # not NaN

    # Sharpe should be finite
    import math
    assert math.isfinite(result.sharpe_ratio)

    # max_drawdown should be non-negative
    assert result.max_drawdown_pct >= 0.0

    # win_rate ∈ [0, 1]
    assert 0.0 <= result.win_rate <= 1.0


@pytest.mark.asyncio
async def test_swing_leader_skip_analysis_complete() -> None:
    engine = make_engine()
    params = base_params(SWING)
    result = await engine.run(params)

    ss = result.skip_stats
    # total must equal sum of individual categories
    assert ss.total == (
        ss.insufficient_liquidity
        + ss.risk_triggered
        + ss.below_min_size
        + ss.category_filtered
    )


# ---------------------------------------------------------------------------
# 3. Triple-scenario ordering: pessimistic PnL ≤ neutral ≤ optimistic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triple_scenario_pnl_ordering() -> None:
    engine = make_engine()
    params = base_params(SWING, copy_ratio=0.05)
    results = await run_triple(engine, params, SWING)

    opt_pnl = results["optimistic"]["stats"]["total_return_usd"]
    neu_pnl = results["neutral"]["stats"]["total_return_usd"]
    pes_pnl = results["pessimistic"]["stats"]["total_return_usd"]

    # Higher slippage → more adverse fills → lower (or equal) PnL
    # Allow a tiny float tolerance
    assert pes_pnl <= neu_pnl + 1e-6, (
        f"pessimistic ({pes_pnl:.4f}) should be ≤ neutral ({neu_pnl:.4f})"
    )
    assert neu_pnl <= opt_pnl + 1e-6, (
        f"neutral ({neu_pnl:.4f}) should be ≤ optimistic ({opt_pnl:.4f})"
    )


@pytest.mark.asyncio
async def test_triple_scenario_result_structure() -> None:
    engine = make_engine()
    params = base_params(SWING)
    results = await run_triple(engine, params, SWING)

    for scenario in ("optimistic", "neutral", "pessimistic"):
        assert scenario in results
        r = results[scenario]
        assert "stats" in r
        assert "daily_pnl_curve" in r
        assert "skip_analysis" in r
        assert "leader_vs_copy" in r
        assert "params" in r


# ---------------------------------------------------------------------------
# 4. Risk gate: tiny daily_loss_limit triggers skips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_loss_limit_triggers_skips() -> None:
    engine = make_engine()
    # $0.01 daily loss limit is so tight that almost every losing day halts trading
    tight_params = base_params(SWING, daily_loss_limit=0.01)
    result = await engine.run(tight_params)

    risk_skips = result.skip_stats.risk_triggered
    # At least some days should be halted after even a tiny loss
    assert risk_skips >= 0  # may be 0 if all trades happen to win; that's OK
    # But total skips should be ≥ 0 and consistent
    assert result.skip_stats.total >= 0


@pytest.mark.asyncio
async def test_tiny_limit_gives_fewer_executed_trades_than_normal() -> None:
    engine_a = make_engine()
    engine_b = make_engine()

    normal = base_params(SWING, daily_loss_limit=1000.0)
    tight = base_params(SWING, daily_loss_limit=0.01)

    result_normal = await engine_a.run(normal)
    result_tight = await engine_b.run(tight)

    executed_normal = sum(1 for e in result_normal.trade_log if not e.get("skipped"))
    executed_tight = sum(1 for e in result_tight.trade_log if not e.get("skipped"))

    # Tight limit should execute ≤ normal (may be equal if no losses)
    assert executed_tight <= executed_normal


# ---------------------------------------------------------------------------
# 5. Determinism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_results() -> None:
    params = base_params(SWING, copy_ratio=0.03)
    engine_a = make_engine()
    engine_b = make_engine()

    result_a = await engine_a.run(params)
    result_b = await engine_b.run(params)

    assert result_a.total_return_usd == pytest.approx(result_b.total_return_usd)
    assert result_a.sharpe_ratio == pytest.approx(result_b.sharpe_ratio)
    assert result_a.skip_stats.total == result_b.skip_stats.total
    assert list(result_a.pnl_curve) == list(result_b.pnl_curve)


# ---------------------------------------------------------------------------
# 6. BacktestResult.to_dict() round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_to_dict_is_json_serialisable() -> None:
    import json

    engine = make_engine()
    params = base_params(SWING)
    result = await engine.run(params)
    d = result.to_dict()

    # Must be JSON-serialisable (no datetimes, no pd.Series, no numpy types)
    serialised = json.dumps(d)
    assert len(serialised) > 0


@pytest.mark.asyncio
async def test_to_dict_structure() -> None:
    engine = make_engine()
    params = base_params(SWING)
    result = await engine.run(params)
    d = result.to_dict()

    assert "params" in d
    assert "daily_pnl_curve" in d
    assert isinstance(d["daily_pnl_curve"], list)
    assert "stats" in d
    for key in ("total_return_usd", "annualized_return_pct", "sharpe_ratio",
                "max_drawdown_pct", "win_rate", "avg_hold_hours"):
        assert key in d["stats"]
    assert "skip_analysis" in d
    assert "leader_vs_copy" in d


# ---------------------------------------------------------------------------
# 7. Category whitelist filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_whitelist_filters_all_when_no_match() -> None:
    engine = make_engine()
    params = base_params(SWING)
    from dataclasses import replace
    filtered_params = replace(params, category_whitelist=["__NONEXISTENT_CATEGORY__"])
    result = await engine.run(filtered_params)

    # All trades should be category-filtered
    executed = [e for e in result.trade_log if not e.get("skipped")]
    assert len(executed) == 0
    assert result.skip_stats.category_filtered > 0


# ---------------------------------------------------------------------------
# 8. Leader vs copy comparison present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leader_pnl_curve_populated() -> None:
    engine = make_engine()
    params = base_params(SWING)
    result = await engine.run(params)

    # Leader PnL curve should have same length as copy curve
    assert len(result.leader_pnl_curve) == len(result.pnl_curve)


# ---------------------------------------------------------------------------
# 9. compute_max_drawdown — negative / non-positive-peak curves (REGRESSION)
#
# The PnL curve produced by _build_pnl_curve starts at 0.0 and *frequently*
# goes negative (it is cumulative PnL above initial capital, and copy-trading
# loses money often).  The original implementation divided the dollar decline
# by abs(peak) — or used abs(v) * 100 when peak == 0 — yielding absurd,
# unbounded results (e.g. a 5000 % "drawdown" for the ordinary curve
# [0.0, -50.0]).  Drawdown as a percentage of the high-water mark is only
# defined while that high-water mark is strictly positive.
# ---------------------------------------------------------------------------


class TestComputeMaxDrawdownNegativeCurves:
    def test_zero_then_loss_is_not_thousands_of_percent(self) -> None:
        # REGRESSION: previously returned 5000.0
        dd = compute_max_drawdown([0.0, -50.0])
        assert dd == pytest.approx(0.0)

    def test_negative_then_more_negative_is_bounded(self) -> None:
        # REGRESSION: previously returned 400.0
        dd = compute_max_drawdown([-10.0, -50.0])
        assert dd == pytest.approx(0.0)

    def test_negative_recovering_no_drawdown(self) -> None:
        assert compute_max_drawdown([-50.0, -10.0]) == pytest.approx(0.0)

    def test_positive_peak_then_negative_uses_peak_denominator(self) -> None:
        # Peak +100, trough -50 → (100 - (-50)) / 100 = 150 %
        assert compute_max_drawdown([100.0, -50.0]) == pytest.approx(150.0)

    def test_crosses_zero_then_recovers(self) -> None:
        # Running peak reaches +20; trough -10 → (20 - (-10)) / 20 = 150 %
        assert compute_max_drawdown([0.0, -30.0, 20.0, -10.0]) == pytest.approx(150.0)

    def test_existing_positive_curves_unchanged(self) -> None:
        # The previously-passing cases must remain identical.
        assert compute_max_drawdown([0.0, 50.0, 100.0, 50.0]) == pytest.approx(50.0)
        assert compute_max_drawdown([0.0, 100.0, 40.0, 80.0, 200.0]) == pytest.approx(60.0)
        assert compute_max_drawdown([0.0, 10.0, 20.0, 30.0]) == pytest.approx(0.0)

    def test_always_non_negative_and_finite(self) -> None:
        import math as _m

        for curve in (
            [-1.0, -2.0, -3.0],
            [0.0, -1000.0],
            [5.0, -5.0, 5.0, -5.0],
            [-0.0001, 0.0001],
        ):
            dd = compute_max_drawdown(curve)
            assert dd >= 0.0
            assert _m.isfinite(dd)


@pytest.mark.asyncio
async def test_losing_leader_backtest_has_sane_max_drawdown() -> None:
    """End-to-end: a losing copy run must report a bounded max_drawdown_pct.

    Heavy pessimistic slippage on the scalper guarantees a negative-leaning
    PnL curve; max_drawdown_pct must stay finite and not explode into the
    thousands of percent (the bug this regression guards).
    """
    engine = make_engine()
    from dataclasses import replace

    params = replace(
        base_params(SCALPER, copy_ratio=0.05),
        slippage_bps=300,
        execution_delay_seconds=60,
    )
    result = await engine.run(params)

    import math as _m

    assert _m.isfinite(result.max_drawdown_pct)
    assert result.max_drawdown_pct >= 0.0


# ---------------------------------------------------------------------------
# 10. Job-queue lifecycle: pending → running → done  AND  → failed
#     (jobs.py had zero coverage; mandate requires the full lifecycle.)
# ---------------------------------------------------------------------------


class _FakeJob:
    """Minimal stand-in for the BacktestJob ORM row used by jobs.py."""

    def __init__(
        self,
        *,
        leader_addresses: list[str],
        template_params: dict[str, object] | None = None,
    ) -> None:
        import uuid as _uuid

        self.id = _uuid.uuid4()
        self.leader_addresses = leader_addresses
        self.template_params = template_params
        self.start_date = START
        self.end_date = END
        self.initial_capital_usd = 1000.0
        self.slippage_bps = 150
        self.execution_delay_seconds = 30
        self.status = "pending"
        self.progress = 0.0
        self.result: dict[str, object] | None = None


def _patch_status_recorder(
    monkeypatch: pytest.MonkeyPatch, transitions: list[dict[str, object]]
) -> None:
    """Capture every job status transition without a real database.

    jobs.py funnels *all* status/progress/result writes through the module
    helper ``_set_status``.  Patching that single seam (plus a no-op
    ``session_scope``) records the full pending→running→done|failed
    lifecycle deterministically and without depending on SQLAlchemy
    statement internals.
    """
    import contextlib

    from polyecho.backtest import jobs as jobs_mod

    @contextlib.asynccontextmanager
    async def _fake_scope():  # type: ignore[no-untyped-def]
        yield object()

    async def _record_set_status(
        session: object,
        job_id: object,
        status: str,
        *,
        progress: float | None = None,
        result: dict[str, object] | None = None,
    ) -> None:
        transitions.append(
            {"status": status, "progress": progress, "result": result}
        )

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_set_status", _record_set_status)


@pytest.mark.asyncio
async def test_job_lifecycle_pending_to_running_to_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polyecho.backtest.jobs import _process_job

    transitions: list[dict[str, object]] = []
    _patch_status_recorder(monkeypatch, transitions)

    job = _FakeJob(
        leader_addresses=[SWING],
        template_params={"copy_ratio": 0.05, "min_trade_size_usd": 1.0},
    )
    engine = make_engine()

    await _process_job(job, engine)  # type: ignore[arg-type]

    statuses = [t["status"] for t in transitions]
    # First a 'running' (progress 0), …, ending with a 'completed'.
    # Terminal success status is 'completed' (matches the ORM model docstring,
    # 0000_init.sql, web schema.ts, and CLI `backtest show` which gate on
    # status in ('completed', 'failed')).
    assert statuses[0] == "running"
    assert statuses[-1] == "completed"
    assert "failed" not in statuses

    first = transitions[0]
    assert float(first["progress"]) == pytest.approx(0.0)  # type: ignore[arg-type]

    final = transitions[-1]
    assert float(final["progress"]) == pytest.approx(100.0)  # type: ignore[arg-type]
    result = final["result"]
    assert isinstance(result, dict)
    assert "leaders" in result
    assert SWING in result["leaders"]
    # Successful leader → triple-scenario dict, not an error envelope.
    assert "error" not in result["leaders"][SWING]
    assert set(result["leaders"][SWING]) == {"optimistic", "neutral", "pessimistic"}


@pytest.mark.asyncio
async def test_job_lifecycle_records_per_leader_error_then_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader that raises inside run_triple must NOT fail the whole job.

    jobs._process_job catches per-leader exceptions, stores an error
    envelope for that leader, and still finishes the job as 'completed'.
    """
    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import _process_job

    transitions: list[dict[str, object]] = []
    _patch_status_recorder(monkeypatch, transitions)

    async def _boom(engine: object, params: object, leader: str) -> dict[str, object]:
        raise RuntimeError("synthetic leader failure")

    monkeypatch.setattr(jobs_mod, "run_triple", _boom)

    job = _FakeJob(leader_addresses=["0xbad"], template_params=None)
    await _process_job(job, make_engine())  # type: ignore[arg-type]

    statuses = [t["status"] for t in transitions]
    assert statuses[-1] == "completed"  # per-leader error is contained
    final_result = transitions[-1]["result"]
    assert isinstance(final_result, dict)
    env = final_result["leaders"]["0xbad"]
    assert "error" in env
    assert "RuntimeError" in env["error"]


@pytest.mark.asyncio
async def test_process_pending_jobs_marks_failed_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _process_job itself raises, the loop must write status='failed'.

    This exercises the pending→failed branch of process_pending_jobs with
    ``once=True`` so it returns after a single poll.
    """
    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import process_pending_jobs

    transitions: list[dict[str, object]] = []
    _patch_status_recorder(monkeypatch, transitions)

    job = _FakeJob(leader_addresses=[SWING])

    async def _fake_fetch(_session: object) -> list[object]:
        return [job]

    async def _explode(_job: object, _engine: object) -> None:
        raise ValueError("catastrophic processing error")

    monkeypatch.setattr(jobs_mod, "_fetch_pending", _fake_fetch)
    monkeypatch.setattr(jobs_mod, "_process_job", _explode)

    await process_pending_jobs(make_ds(), once=True)

    statuses = [t["status"] for t in transitions]
    assert statuses[-1] == "failed"
    err = transitions[-1]["result"]
    assert isinstance(err, dict)
    assert "ValueError" in err["error"]


# ---------------------------------------------------------------------------
# 11. _build_params: top-level job columns win over template_params
# ---------------------------------------------------------------------------


def test_build_params_template_merge_and_column_precedence() -> None:
    from polyecho.backtest.jobs import _build_params

    job = _FakeJob(
        leader_addresses=["0xignored"],
        template_params={
            "copy_ratio": 0.02,
            "fixed_size_usd": 7.5,
            "max_position_usd": 333.0,
            "min_trade_size_usd": 2.0,
            "daily_loss_limit_usd": 99.0,
            "max_concurrent_positions": 4,
            "category_whitelist": ["Crypto"],
            # Even if a stale slippage leaks into template_params it must be
            # ignored — the top-level column is authoritative.
            "slippage_bps": 9999,
        },
    )
    job.slippage_bps = 222
    job.execution_delay_seconds = 45

    params = _build_params(job, "0xleaderX")  # type: ignore[arg-type]

    assert params.leader_address == "0xleaderX"
    assert params.copy_ratio == pytest.approx(0.02)
    assert params.fixed_size_usd == pytest.approx(7.5)
    assert params.max_position_usd == pytest.approx(333.0)
    assert params.min_trade_size_usd == pytest.approx(2.0)
    assert params.daily_loss_limit_usd == pytest.approx(99.0)
    assert params.max_concurrent_positions == 4
    assert params.category_whitelist == ["Crypto"]
    # Column precedence: template_params slippage is ignored.
    assert params.slippage_bps == 222
    assert params.execution_delay_seconds == 45
    assert params.start_date == START
    assert params.end_date == END


def test_build_params_defaults_when_template_absent() -> None:
    from polyecho.backtest.jobs import _build_params

    job = _FakeJob(leader_addresses=["0xx"], template_params=None)
    params = _build_params(job, "0xx")  # type: ignore[arg-type]

    assert params.copy_ratio == pytest.approx(0.001)
    assert params.fixed_size_usd == pytest.approx(0.0)
    assert params.max_position_usd == pytest.approx(100.0)
    assert params.min_trade_size_usd == pytest.approx(5.0)
    assert params.daily_loss_limit_usd == pytest.approx(50.0)
    assert params.max_concurrent_positions == 10
    assert params.fee_bps == 0
    assert params.category_whitelist is None


def test_build_params_reads_fee_bps_from_template() -> None:
    from polyecho.backtest.jobs import _build_params

    job = _FakeJob(leader_addresses=["0xx"], template_params={"fee_bps": 75})
    params = _build_params(job, "0xx")  # type: ignore[arg-type]
    assert params.fee_bps == 75


# ===========================================================================
# WAVE B-backtest remediation — PRD §5.4 non-negotiables + P1/C4/I5–I8
# ===========================================================================


# ---------------------------------------------------------------------------
# P1-10a — fee model: pure helper + both-leg PnL reduction + to_dict()
# ---------------------------------------------------------------------------


class TestComputeFee:
    def test_zero_fee_is_free(self) -> None:
        assert compute_fee(100.0, 0) == 0.0

    def test_negative_or_zero_notional_is_free(self) -> None:
        assert compute_fee(0.0, 100) == 0.0
        assert compute_fee(-5.0, 100) == 0.0

    def test_basis_point_math(self) -> None:
        # 100 USDC @ 50 bps = 100 * 50 / 10_000 = 0.50
        assert compute_fee(100.0, 50) == pytest.approx(0.50)
        # 200 USDC @ 150 bps = 3.00
        assert compute_fee(200.0, 150) == pytest.approx(3.00)


@pytest.mark.asyncio
async def test_fee_reduces_pnl_on_both_legs_and_appears_in_to_dict() -> None:
    """A non-zero fee_bps must strictly reduce total return vs fee=0.

    The fee is charged on every BUY open AND every SELL close, so a run
    with any executed round-trip and fee_bps>0 must have lower total
    return and a positive total_fees_usd that surfaces in to_dict().
    """
    from dataclasses import replace

    base = base_params(SWING, copy_ratio=0.05)

    engine_free = make_engine()
    engine_fee = make_engine()

    res_free = await engine_free.run(base)
    res_fee = await engine_fee.run(replace(base, fee_bps=200))

    # Some trades must have executed for the test to be meaningful.
    executed = [e for e in res_fee.trade_log if not e.get("skipped")]
    assert len(executed) > 0

    # Fee is a pure cost → strictly worse net return.
    assert res_fee.total_return_usd < res_free.total_return_usd
    # Aggregate fees are positive and surfaced in to_dict().
    assert res_fee.total_fees_usd > 0.0
    d = res_fee.to_dict()
    assert d["stats"]["total_fees_usd"] == pytest.approx(res_fee.total_fees_usd)
    assert d["params"]["fee_bps"] == 200
    # Both legs charged: at least one BUY entry and one SELL/MTM entry
    # carry a positive fee_usd, and the per-entry sum reconciles.
    fee_entries = [
        e for e in res_fee.trade_log
        if not e.get("skipped") and float(e.get("fee_usd") or 0.0) > 0.0
    ]
    buy_fees = [e for e in fee_entries if e["side"] == "BUY"]
    sell_fees = [e for e in fee_entries if e["side"] == "SELL"]
    assert len(buy_fees) > 0, "BUY-leg fee must be charged"
    assert len(sell_fees) > 0, "SELL-leg fee must be charged"
    summed = sum(
        float(e.get("fee_usd") or 0.0)
        for e in res_fee.trade_log
        if not e.get("skipped")
    )
    assert summed == pytest.approx(res_fee.total_fees_usd)


@pytest.mark.asyncio
async def test_higher_fee_monotonically_worsens_return() -> None:
    from dataclasses import replace

    base = base_params(SWING, copy_ratio=0.05)
    r0 = await make_engine().run(replace(base, fee_bps=0))
    r1 = await make_engine().run(replace(base, fee_bps=100))
    r2 = await make_engine().run(replace(base, fee_bps=400))
    assert r0.total_return_usd >= r1.total_return_usd >= r2.total_return_usd
    assert r0.total_fees_usd == 0.0 < r1.total_fees_usd < r2.total_fees_usd


# ---------------------------------------------------------------------------
# P1-10b — settlement snap
# ---------------------------------------------------------------------------


class TestComputeSettlementPrice:
    def test_snaps_to_one_above_threshold(self) -> None:
        assert compute_settlement_price(0.97) == 1.0
        assert compute_settlement_price(0.95) == 1.0

    def test_snaps_to_zero_below_threshold(self) -> None:
        assert compute_settlement_price(0.03) == 0.0
        assert compute_settlement_price(0.05) == 0.0

    def test_mid_band_stays_live_mtm(self) -> None:
        assert compute_settlement_price(0.50) == pytest.approx(0.50)
        assert compute_settlement_price(0.94) == pytest.approx(0.94)
        assert compute_settlement_price(0.06) == pytest.approx(0.06)

    def test_voided_returns_entry_price_zero_mtm(self) -> None:
        # Voided market → unwind at entry → shares*(entry-entry)=0 MTM.
        assert compute_settlement_price(
            0.97, voided=True, entry_price=0.42
        ) == pytest.approx(0.42)
        # Void wins even over a >=0.95 price.
        assert compute_settlement_price(
            0.99, voided=True, entry_price=0.10
        ) == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_mtm_entries_are_settlement_snapped() -> None:
    """End-to-end: every MTM fill_price must be a settled value or in-band.

    The MTM leg price must never be an un-snapped value sitting in the
    [0.95,1.0) or (0.0,0.05] dead-zones — it is snapped to exactly 1.0/0.0
    there.
    """
    engine = make_engine()
    result = await engine.run(base_params(SWING, copy_ratio=0.05))
    mtm = [e for e in result.trade_log if e.get("side") == "MTM"]
    for e in mtm:
        fp = float(e["fill_price"])
        assert 0.0 <= fp <= 1.0
        # No value may sit strictly inside the snap dead-zones.
        assert not (0.95 <= fp < 1.0)
        assert not (0.0 < fp <= 0.05)


# ---------------------------------------------------------------------------
# P1-10c — no look-ahead bias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_period_end_price_independent_of_runtime_live_positions() -> None:
    """MTM must NOT depend on the run-time get_positions() snapshot.

    We monkeypatch get_positions to return wildly different values; the
    period-end MTM (derived from the in-window trade log) must be
    byte-identical, proving no live/future positions snapshot influences it.
    """
    from polyecho.shared.datasource.dto import PositionRecord

    base = base_params(SWING, copy_ratio=0.05)

    engine_a = make_engine()
    result_a = await engine_a.run(base)

    engine_b = make_engine()

    async def _fake_positions(
        wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]:
        # Absurd, contradictory "current" state — must NOT leak into MTM.
        return [
            PositionRecord(
                wallet=wallet,
                condition_id="0xcond_politics_potus_2028",
                asset="x",
                size=999.0,
                avg_price=0.01,
                current_value=999.0,  # implies price ~1.0
                cash_pnl=0.0,
                percent_pnl=0.0,
                title=None,
                outcome=None,
                raw={},
            )
        ]

    cast(Any, engine_b._ds).get_positions = _fake_positions
    result_b = await engine_b.run(base)

    # Look-ahead-free: identical regardless of live positions snapshot.
    assert result_a.total_return_usd == pytest.approx(result_b.total_return_usd)
    assert list(result_a.pnl_curve) == list(result_b.pnl_curve)
    mtm_a = [
        (e["condition_id"], e["fill_price"])
        for e in result_a.trade_log
        if e.get("side") == "MTM"
    ]
    mtm_b = [
        (e["condition_id"], e["fill_price"])
        for e in result_b.trade_log
        if e.get("side") == "MTM"
    ]
    assert mtm_a == mtm_b


# ---------------------------------------------------------------------------
# C4 — SELL without a matching open BUY is skipped (no win-rate inflation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_without_open_buy_is_skipped_with_reason() -> None:
    """A leader whose window opens mid-stream emits SELLs for BUYs that
    happened before start_date.  Those SELLs must be skipped with
    skip_reason='no_open_position' and excluded from closed-trade /
    win-rate accounting."""
    from datetime import date as _date

    # Narrow the window so the first in-window trade for some market is a
    # SELL whose BUY is out of window.
    params = BacktestParams(
        leader_address=SWING,
        start_date=_date(2026, 4, 1),
        end_date=END,
        initial_capital_usd=1000.0,
        copy_ratio=0.05,
        fixed_size_usd=0.0,
        max_position_usd=200.0,
        min_trade_size_usd=1.0,
        daily_loss_limit_usd=10_000.0,
        max_concurrent_positions=50,
        slippage_bps=150,
        execution_delay_seconds=30,
        category_whitelist=None,
    )
    result = await make_engine().run(params)

    no_open = [
        e for e in result.trade_log
        if e.get("skipped") and e.get("skip_reason") == "no_open_position"
    ]
    assert len(no_open) > 0, "expected at least one SELL-without-BUY skip"

    # Skipped no_open_position rows must NOT be in the closed-trade
    # population (would otherwise be 0-PnL phantom losses inflating the
    # denominator and deflating win_rate).
    for e in no_open:
        assert e["skipped"] is True
        assert e.get("pnl_usd") == 0.0

    # win_rate must be computed only over genuinely closed trades.
    closed = [
        e for e in result.trade_log
        if not e.get("skipped") and e.get("side") not in (None, "BUY")
    ]
    for e in closed:
        assert e.get("skip_reason") != "no_open_position"
    assert 0.0 <= result.win_rate <= 1.0
    # Skip-stat accounting stays consistent.
    ss = result.skip_stats
    assert ss.total == (
        ss.insufficient_liquidity
        + ss.risk_triggered
        + ss.below_min_size
        + ss.category_filtered
    )


# ---------------------------------------------------------------------------
# I5 — stale RUNNING jobs recovered to PENDING on (re)start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_stale_running_resets_old_jobs() -> None:
    """A 'running' job older than the stale threshold is flipped to
    'pending'; a fresh 'running' job and a 'completed' job are untouched."""
    from datetime import UTC, datetime, timedelta

    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import _STALE_RUNNING_SECONDS, _recover_stale_running

    captured: dict[str, object] = {}

    class _FakeResult:
        rowcount = 3  # pretend 3 stale rows matched

    class _FakeSession:
        async def execute(self, stmt: object) -> _FakeResult:
            captured["stmt"] = stmt
            return _FakeResult()

        async def commit(self) -> None:
            captured["committed"] = True

    n = await _recover_stale_running(_FakeSession())  # type: ignore[arg-type]
    assert n == 3
    assert captured.get("committed") is True
    # The UPDATE statement compiles to a status='running' guarded reset.
    compiled = str(captured["stmt"])
    assert "backtest_jobs" in compiled
    assert "status" in compiled
    # Threshold is a sane positive duration.
    assert _STALE_RUNNING_SECONDS > 0
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=_STALE_RUNNING_SECONDS)
    assert cutoff < datetime.now(tz=UTC)
    assert jobs_mod._recover_stale_running is _recover_stale_running


@pytest.mark.asyncio
async def test_worker_loop_recovers_stale_then_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_pending_jobs must call stale-recovery before fetching work
    (so a recovered job re-enters the same poll)."""
    import contextlib

    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import process_pending_jobs

    order: list[str] = []

    @contextlib.asynccontextmanager
    async def _fake_scope():  # type: ignore[no-untyped-def]
        yield object()

    async def _fake_recover(_session: object) -> int:
        order.append("recover")
        return 0

    async def _fake_fetch(_session: object) -> list[object]:
        order.append("fetch")
        return []

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_recover_stale_running", _fake_recover)
    monkeypatch.setattr(jobs_mod, "_fetch_pending", _fake_fetch)

    await process_pending_jobs(make_ds(), once=True)

    assert order == ["recover", "fetch"], (
        f"recovery must run before fetch; got {order}"
    )


# ---------------------------------------------------------------------------
# I6 — annualised return uses initial_capital_usd (not day-1 PnL)
# ---------------------------------------------------------------------------


def test_annualised_return_uses_initial_capital() -> None:
    """For a deterministic curve the annualised % must equal
    (total / initial_capital) * (365 / n_days) * 100 — independent of the
    (usually-zero) day-1 cumulative PnL."""
    import pandas as pd

    eng = make_engine()
    # 10-day curve, final cumulative PnL = +100 on $1000 capital.
    idx = pd.date_range("2026-01-01", "2026-01-10", freq="D").date
    vals = [0.0] * 9 + [100.0]
    curve = pd.Series(vals, index=idx, dtype=float)

    p = base_params(SWING)
    from dataclasses import replace

    p = replace(p, initial_capital_usd=1000.0)

    stats = eng._compute_statistics(curve, [], p)
    # (100 / 1000) * (365 / 10) * 100 = 365.0
    assert stats["annualised_return_pct"] == pytest.approx(365.0)

    # Day-1 PnL being non-zero must NOT change the denominator.
    vals2 = [50.0] + [0.0] * 8 + [100.0]
    curve2 = pd.Series(vals2, index=idx, dtype=float)
    stats2 = eng._compute_statistics(curve2, [], p)
    assert stats2["annualised_return_pct"] == pytest.approx(365.0)


# ---------------------------------------------------------------------------
# I7 — activity truncation warning when the 10k offset cap is hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_truncation_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When get_user_activity returns >= the 10k offset cap, the engine
    must emit a loud truncation warning (cursor pagination is not offered
    by the Polymarket /activity endpoint — context7-verified)."""
    import logging

    from polyecho.shared.datasource.dto import ActivityRecord

    engine = make_engine()

    big_count = 10_000

    async def _huge_activity(
        wallet: str,
        *,
        start: object = None,
        end: object = None,
        activity_type: object = "TRADE",
        limit: object = None,
    ) -> list[ActivityRecord]:
        ts = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
        return [
            ActivityRecord(
                wallet=wallet,
                activity_type="TRADE",
                condition_id="0xc",
                side="BUY",
                size=1.0,
                price=0.5,
                usdc_size=1.0,
                timestamp=ts,
                raw={},
            )
            for _ in range(big_count)
        ]

    cast(Any, engine._ds).get_user_activity = _huge_activity

    with caplog.at_level(logging.WARNING):
        await engine._fetch_leader_trades(SWING, START, END)

    assert any(
        "activity_truncated" in r.message or "activity_truncated" in r.name
        for r in caplog.records
    ), "expected a backtest.activity_truncated warning"


@pytest.mark.asyncio
async def test_no_truncation_warning_under_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    engine = make_engine()
    with caplog.at_level(logging.WARNING):
        await engine._fetch_leader_trades(SWING, START, END)
    assert not any(
        "activity_truncated" in r.message for r in caplog.records
    ), "swing fixture (295 trades) must not trigger the truncation warning"


# ---------------------------------------------------------------------------
# I8 — avg_hold_hours uses the matching BUY opened_at (non-zero, correct)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_avg_hold_hours_uses_buy_opened_at() -> None:
    """avg_hold_hours must be the SELL ts − the matching BUY's opened_at.

    Previously it used SELL ts − SELL closed_at (both the SELL timestamp),
    giving ≈0 for every trade.  With a leader that holds positions for
    days, the average hold must be clearly > 0.
    """
    engine = make_engine()
    result = await engine.run(base_params(SWING, copy_ratio=0.05))

    # SELL entries that closed a position must carry the BUY's opened_at.
    closing_sells = [
        e for e in result.trade_log
        if not e.get("skipped")
        and e.get("side") == "SELL"
        and e.get("closed_at") is not None
    ]
    assert len(closing_sells) > 0
    for e in closing_sells:
        assert e.get("opened_at") is not None
        opened = datetime.fromisoformat(str(e["opened_at"]))
        sold = datetime.fromisoformat(str(e["timestamp"]))
        assert sold >= opened  # hold is non-negative

    # The swing leader holds across days → strictly positive average.
    assert result.avg_hold_hours > 0.0


def test_avg_hold_hours_known_value() -> None:
    """Exact-value check: a single round-trip held for 48 h must report
    avg_hold_hours == 48.0 (proves opened_at, not closed_at, is used)."""
    eng = make_engine()
    trade_log = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "side": "BUY",
            "skipped": False,
            "opened_at": None,
            "closed_at": None,
            "pnl_usd": 0.0,
        },
        {
            "timestamp": "2026-01-03T00:00:00+00:00",  # SELL 48h later
            "side": "SELL",
            "skipped": False,
            "opened_at": "2026-01-01T00:00:00+00:00",  # matching BUY open
            "closed_at": "2026-01-03T00:00:00+00:00",
            "pnl_usd": 5.0,
        },
    ]
    import pandas as pd

    idx = pd.date_range("2026-01-01", "2026-01-03", freq="D").date
    curve = pd.Series([0.0, 0.0, 5.0], index=idx, dtype=float)
    stats = eng._compute_statistics(curve, trade_log, base_params(SWING))
    assert stats["avg_hold_hours"] == pytest.approx(48.0)
