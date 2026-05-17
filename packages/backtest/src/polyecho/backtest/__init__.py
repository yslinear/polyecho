"""polyecho.backtest — Backtest engine.

Simulates copy-trading PnL with slippage, delay, and liquidity models (PRD §5.4).
"""

from polyecho.backtest.engine import BacktestEngine, BacktestParams, BacktestResult

__all__ = ["BacktestEngine", "BacktestParams", "BacktestResult"]
