# Datasource Fixtures

JSON fixture data bundled with `polyecho-shared` for deterministic, no-network
testing and default development runtime (`POLYECHO_DATASOURCE=fixture`).

## Directory layout

```
fixtures/
├── leaderboard.json            12 entries, rank 1-12
├── markets.json                8 markets sorted by volume_24h desc
├── activity/
│   ├── 0xswing1…001.json       Swing archetype   (295 trades, 90 d)
│   ├── 0xscalper2…002.json     Scalper archetype (2700 trades, 90 d)
│   ├── 0xdaytrader3…003.json   Day-trader        (1134 trades, 90 d)
│   ├── 0xposition4…004.json    Position holder   (12 trades, 90 d)
│   ├── 0xdrifted5…005.json     Drifted leader    (439 trades, 90 d)
│   └── 0xinsider6…006.json     Insider suspect   (24 trades, 90 d)
├── positions/
│   └── <wallet>.json           Open positions per wallet (1-4 rows each)
└── market_holders/
    └── <condition_id>.json     Top-10 holders per market (8 markets)
```

## Wallet archetypes

| Wallet (prefix)  | Archetype       | trades/day | avg hold  | Discovery signal            |
|------------------|-----------------|------------|-----------|-----------------------------|
| `0xswing1…001`   | Swing sweet-spot| 1–5        | 1–7 d     | steady PnL, copyable        |
| `0xscalper2…002` | Scalper         | 20–40      | < 1 h     | replicability ≈ 0           |
| `0xdaytrader3…003` | Day Trader    | 5–20       | 1–8 h     | marginal copyability        |
| `0xposition4…004` | Position holder | < 1       | > 1 week  | copyable, long confirmation |
| `0xdrifted5…005` | Drifted         | swing 83 d then scalper 7 d | mixed | triggers drift alert |
| `0xinsider6…006` | Insider suspect | < 1/5 d   | near-resolution | trips `insider_suspect` flag |

## JSON schemas

### leaderboard.json

Array of objects:

```json
{
  "rank": 1,
  "wallet": "0x…",
  "username": "SwingKing",
  "pnl": 142300.50,
  "volume": 980000.00,
  "verified": true
}
```

### activity/<wallet>.json

Array of activity objects (sorted ascending by `timestamp`):

```json
{
  "wallet": "0x…",
  "type": "TRADE",
  "conditionId": "0xcond_…",
  "side": "BUY",
  "size": 120.5,
  "price": 0.4812,
  "usdcSize": 57.99,
  "timestamp": 1715000000,
  "title": "Market: …",
  "outcome": "Yes"
}
```

`type` is one of `TRADE` | `REDEEM`. Insider fixture also includes `REDEEM` rows.

### positions/<wallet>.json

Array of position objects:

```json
{
  "wallet": "0x…",
  "conditionId": "0xcond_…",
  "asset": "tok-…-yes",
  "size": 300.0,
  "avgPrice": 0.45,
  "currentValue": 144.0,
  "cashPnl": 9.0,
  "percentPnl": 6.67,
  "title": "Market: …",
  "outcome": "Yes"
}
```

### markets.json

Array of market summaries sorted by `volume_24h` descending:

```json
{
  "condition_id": "0xcond_…",
  "market_id": "mkt-…",
  "question": "Will …?",
  "volume_24h": 4200000.00,
  "end_date": "2028-11-04T04:00:00+00:00",
  "tokens": [
    {"token_id": "tok-…-yes", "outcome": "Yes", "price": 0.48},
    {"token_id": "tok-…-no",  "outcome": "No",  "price": 0.52}
  ]
}
```

### market_holders/<condition_id>.json

Array of position objects (same schema as positions), sorted by `size` desc,
top 10 holders. Files are named exactly after the condition ID
(e.g. `0xcond_politics_potus_2028.json`).

## Extending fixtures

1. Add a new `activity/<wallet>.json` file.
2. Optionally add matching `positions/<wallet>.json`.
3. Add the wallet to `leaderboard.json` if it should appear in rankings.
4. No code changes required — `FixtureDataSource` picks up files automatically.
