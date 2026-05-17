# polyecho-discovery

Discovery-worker service — finds, scores, and tracks Polymarket leader candidates
from four sources (PRD §5.2/§5.3).

## Architecture

```
polyecho.discovery
├── __main__.py          Entry point (--once | scheduled loop)
├── __init__.py          Public API re-exports
├── frequency.py         FrequencyStats, StrategyType, compute/classify/detect_drift
├── scoring.py           profit_score, replicability_score, final_score
├── riskflags.py         detect_risk_flags (insider_suspect → auto-reject)
├── worker.py            DiscoveryWorker (4 sources + DB persistence)
└── smart_money_seed.json  Editable wallet seed list (weekly sync)
```

## Four discovery sources (PRD §5.2)

| Source                    | Cadence | Method                                       |
|---------------------------|---------|----------------------------------------------|
| Leaderboard               | Daily   | `get_leaderboard(time_period="MONTH", limit=200)` |
| Hot-market top holders    | Daily   | `get_markets_by_volume` → `get_market_top_holders` |
| Large new positions       | Hourly  | Scan known wallet pool for usdc_size > $5K   |
| Smart-money seed          | Weekly  | Read `smart_money_seed.json`                 |

## Scoring (PRD §5.2)

```
final_score = profit_score × (replicability_score / 100)
```

**profit_score** (0–100): PnL 25% + win_rate 20% + Sharpe 20% + max_drawdown 15%
+ consistency 10% + specialization 10%

**replicability_score** (0–100): multiplicative product of:
- frequency_match (median gap ≥ 30 min → 1.0)
- capital_match (follower capital / concurrent positions ≥ $10)
- burstiness_factor (1 − burstiness)
- time_overlap (leader active hours ∩ follower UTC 00–14)

## Strategy classification (PRD §5.3)

| Type            | trades/day | avg hold  | Copyable  |
|-----------------|------------|-----------|-----------|
| Scalper         | > 20       | < 1 h     | No        |
| Day Trader      | 5–20       | 1–8 h     | Marginal  |
| **Swing**       | **1–5**    | **1–7 d** | **Yes ✅** |
| Position        | < 1        | > 7 d     | Yes       |
| Hold-to-Resolve | any        | to result | Yes       |

Strategy drift is detected when 30d and 7d classifications differ → inserts
an `events` row (severity=warning) for the push-sender.

## Risk flags (PRD §8.1)

| Flag               | Effect        | Heuristic                                          |
|--------------------|---------------|----------------------------------------------------|
| `insider_suspect`  | **Auto-reject** | Rare+large+near-resolution wins, high win-rate    |
| `strategy_drift`   | Event inserted | 30d ≠ 7d strategy type                            |
| `new_account`      | Warning        | History < 30 days                                  |
| `low_liquidity`    | Warning        | > 60% of positions < $100 current value           |
| `high_concentration` | Warning     | Single market > 50% of total volume               |

## Running

```bash
# One-shot pass (CI / manual)
python -m polyecho.discovery --once

# Scheduled service (daily + hourly + weekly loops)
python -m polyecho.discovery
```

Default datasource is `FixtureDataSource` (no network).
Set `POLYECHO_DATASOURCE=live` to use the real Polymarket API.

## Tests

```bash
cd packages/discovery
pytest tests/ -v
```

Tests use `FixtureDataSource` and mock `session_scope` — no DB required.
All six fixture archetypes are covered (swing, scalper, drifted, insider,
score-history persistence, worker smoke test).

## Smart-money seed

Edit `src/polyecho/discovery/smart_money_seed.json` directly.
Format:

```json
[
  { "wallet": "0x...", "comment": "why you added this wallet" }
]
```

The weekly `sync_smart_money_seed()` run picks up changes automatically.

## Dependencies

- `polyecho-shared` (datasource, ORM models, db session)
- `numpy >= 1.26` (burstiness / CV computation)
- `structlog` (via shared)
