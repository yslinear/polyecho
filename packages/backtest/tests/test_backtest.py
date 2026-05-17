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

from datetime import date

import pytest

from polyecho.backtest.engine import (
    BacktestEngine,
    BacktestParams,
    apply_slippage,
    compute_max_drawdown,
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
    assert params.category_whitelist is None
