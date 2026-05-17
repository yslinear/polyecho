"""Pre-defined backtest scenario presets.

PRD reference: §5.4 (three-scenario side-by-side comparison).

Three scenarios (PRD §5.4):
    Optimistic  — slippage 50 bps,  delay 10 s
    Neutral     — slippage 150 bps, delay 30 s   (= BacktestParams defaults)
    Pessimistic — slippage 300 bps, delay 60 s
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from polyecho.backtest.engine import BacktestEngine, BacktestParams, BacktestResult


def optimistic_params(base: BacktestParams) -> BacktestParams:
    """Return a copy of *base* with optimistic slippage/delay settings.

    PRD §5.4: slippage_bps=50, execution_delay_seconds=10.

    Args:
        base: Base BacktestParams with all required fields set.

    Returns:
        New BacktestParams with overridden slippage/delay.
    """
    return replace(base, slippage_bps=50, execution_delay_seconds=10)


def neutral_params(base: BacktestParams) -> BacktestParams:
    """Return a copy of *base* with neutral (default) slippage/delay settings.

    PRD §5.4: slippage_bps=150, execution_delay_seconds=30.
    These match the BacktestParams dataclass defaults.

    Args:
        base: Base BacktestParams.

    Returns:
        New BacktestParams with default slippage/delay.
    """
    return replace(base, slippage_bps=150, execution_delay_seconds=30)


def pessimistic_params(base: BacktestParams) -> BacktestParams:
    """Return a copy of *base* with pessimistic slippage/delay settings.

    PRD §5.4: slippage_bps=300, execution_delay_seconds=60.

    Args:
        base: Base BacktestParams.

    Returns:
        New BacktestParams with worst-case slippage/delay.
    """
    return replace(base, slippage_bps=300, execution_delay_seconds=60)


async def run_triple(
    engine: BacktestEngine,
    params: BacktestParams,
    leader: str,
) -> dict[str, Any]:
    """Run optimistic, neutral, and pessimistic backtests side-by-side.

    PRD §5.4 — "三組情境並排".

    Args:
        engine: Shared BacktestEngine instance (shares the same DataSource).
        params: Base BacktestParams; slippage_bps / execution_delay_seconds
                will be overridden per scenario.
        leader: Leader wallet address (passed to engine.run as leader_address).

    Returns:
        Dict with keys "optimistic", "neutral", "pessimistic", each mapped to
        the BacktestResult.to_dict() serialisation so callers can store it
        directly as JSONB.
    """
    results: dict[str, Any] = {}
    for name, scenario_params in [
        ("optimistic", optimistic_params(params)),
        ("neutral", neutral_params(params)),
        ("pessimistic", pessimistic_params(params)),
    ]:
        result: BacktestResult = await engine.run(
            scenario_params, leader_address=leader
        )
        results[name] = result.to_dict()
    return results


# ---------------------------------------------------------------------------
# Legacy alias kept for backward-compat with existing callers / tests
# ---------------------------------------------------------------------------

async def run_triple_scenario(base: BacktestParams) -> dict[str, BacktestResult]:
    """Legacy entry-point: instantiates its own engine with FixtureDataSource.

    Prefer run_triple(engine, params, leader) for production use where a
    DataSource is already configured.

    Args:
        base: Base BacktestParams with leader_address set.

    Returns:
        Dict mapping scenario name → BacktestResult (object, not dict).
    """
    from polyecho.shared.datasource import get_datasource

    ds = get_datasource()
    engine = BacktestEngine(datasource=ds)
    results: dict[str, BacktestResult] = {}
    for name, scenario_params in [
        ("optimistic", optimistic_params(base)),
        ("neutral", neutral_params(base)),
        ("pessimistic", pessimistic_params(base)),
    ]:
        results[name] = await engine.run(
            scenario_params, leader_address=base.leader_address
        )
    return results
