polyecho-backtest: copy-trade backtest engine with slippage/delay/risk models (PRD §5.4).

## Modules

| File | Purpose |
|---|---|
| `engine.py` | `BacktestEngine`, `BacktestParams`, `BacktestResult`, pure helpers |
| `scenarios.py` | Optimistic/neutral/pessimistic param presets; `run_triple()` |
| `jobs.py` | `process_pending_jobs()` — polls `backtest_jobs` table, dispatches engine |
| `__main__.py` | `python -m polyecho.backtest [--once]` entry-point |

## Quick start

```python
import asyncio
from datetime import date
from polyecho.shared.datasource import get_datasource
from polyecho.backtest.engine import BacktestEngine, BacktestParams

async def main():
    ds = get_datasource()           # fixture by default
    engine = BacktestEngine(datasource=ds)
    params = BacktestParams(
        leader_address="0xswing1000000000000000000000000000000001",
        start_date=date(2026, 2, 16),
        end_date=date(2026, 4, 28),
        initial_capital_usd=1000.0,
        copy_ratio=0.05,
        max_position_usd=200.0,
        min_trade_size_usd=1.0,
        daily_loss_limit_usd=50.0,
    )
    result = await engine.run(params)
    print(result.total_return_usd, result.sharpe_ratio)

asyncio.run(main())
```

## Scenarios

```python
from polyecho.backtest.scenarios import run_triple
results = await run_triple(engine, params, leader_address)
# keys: "optimistic" (50bps/10s), "neutral" (150bps/30s), "pessimistic" (300bps/60s)
```

## Modeling assumptions

- **ASSUMPTION-DELAY**: No intra-bar order-book data; execution delay modelled as extra adverse slippage at 0.5 bps/s (30 s → +15 bps on top of `slippage_bps`).
- **ASSUMPTION-LIQ**: Liquidity gate rejects copy size > 10× leader's notional (thin-market proxy).
- **ASSUMPTION-MTM**: End-of-period mark-to-market uses `PositionRecord.current_value / size`; falls back to fill price (zero MTM PnL) if no position data.
- **ASSUMPTION-SHARPE**: Risk-free rate = 0; annualised over 252 trading days.
- **ASSUMPTION-ANNUALIZE**: Return annualisation uses 365 calendar days.
- **ASSUMPTION-COPY-SIZE**: `fixed_size_usd > 0` overrides `copy_ratio` entirely (mutually exclusive per PRD §5.6).

## Worker

```bash
# Process all pending jobs then exit
python -m polyecho.backtest --once

# Loop forever (poll every 10 s)
python -m polyecho.backtest
```

## Tests

```bash
POLYECHO_DATASOURCE=fixture pytest packages/backtest -q
```

25 tests covering: pure helpers (slippage, Sharpe, max-drawdown), swing leader stats, triple-scenario ordering, risk-gate skips, determinism, JSON serialisation, category whitelist, leader-vs-copy comparison.
