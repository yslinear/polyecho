"""Pre-defined backtest scenario presets.

PRD reference: §5.4 (three-scenario side-by-side comparison).

Three scenarios (PRD §5.4):
    Optimistic  — slippage 50 bps,  delay 10s
    Neutral     — slippage 150 bps, delay 30s   (= BacktestParams defaults)
    Pessimistic — slippage 300 bps, delay 60s
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from polyecho.backtest.engine import BacktestEngine, BacktestParams, BacktestResult


def optimistic_params(base: BacktestParams) -> BacktestParams:
    """Return a copy of *base* with optimistic slippage/delay settings.

    PRD §5.4: slippage_bps=50, execution_delay_seconds=10.

    Args:
        base: Base :class:`BacktestParams` with all required fields set.

    Returns:
        New :class:`BacktestParams` with overridden slippage/delay.
    """
    return replace(base, slippage_bps=50, execution_delay_seconds=10)


def neutral_params(base: BacktestParams) -> BacktestParams:
    """Return a copy of *base* with neutral (default) slippage/delay settings.

    PRD §5.4: slippage_bps=150, execution_delay_seconds=30.
    These match the :class:`BacktestParams` dataclass defaults.

    Args:
        base: Base :class:`BacktestParams`.

    Returns:
        New :class:`BacktestParams` with default slippage/delay.
    """
    return replace(base, slippage_bps=150, execution_delay_seconds=30)


def pessimistic_params(base: BacktestParams) -> BacktestParams:
    """Return a copy of *base* with pessimistic slippage/delay settings.

    PRD §5.4: slippage_bps=300, execution_delay_seconds=60.

    Args:
        base: Base :class:`BacktestParams`.

    Returns:
        New :class:`BacktestParams` with worst-case slippage/delay.
    """
    return replace(base, slippage_bps=300, execution_delay_seconds=60)


async def run_triple_scenario(base: BacktestParams) -> dict[str, BacktestResult]:
    """Run optimistic, neutral, and pessimistic backtests and return all three.

    PRD §5.4 — "三組情境並排":
    Results are keyed ``"optimistic"``, ``"neutral"``, ``"pessimistic"``
    and are intended to be displayed side-by-side in the Dashboard.

    Args:
        base: Base :class:`BacktestParams` with leader, dates, and capital set.
              The ``slippage_bps`` and ``execution_delay_seconds`` fields will
              be overridden per scenario.

    Returns:
        Dict mapping scenario name to :class:`BacktestResult`.

    Raises:
        NotImplementedError: until Phase 2 (delegates to BacktestEngine.run).
    """
    engine = BacktestEngine()
    results: dict[str, BacktestResult] = {}
    for name, params in [
        ("optimistic", optimistic_params(base)),
        ("neutral", neutral_params(base)),
        ("pessimistic", pessimistic_params(base)),
    ]:
        results[name] = await engine.run(params)
    return results
