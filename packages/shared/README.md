# polyecho-shared

Shared Python library for the PolyEcho monorepo. Provides the building blocks
used by every PolyEcho service: Polymarket CLOB client wrapper, SQLAlchemy ORM
models, async database layer, AES-256-GCM crypto helpers, pydantic-settings
configuration, and structlog JSON logging.

## Package layout

```
packages/shared/
├── pyproject.toml
└── src/polyecho/shared/
    ├── __init__.py
    ├── config.py           # pydantic-settings Settings (POLYECHO_ prefix)
    ├── db.py               # SQLAlchemy 2.0 async engine, Base, session_scope
    ├── crypto.py           # AES-256-GCM encrypt/decrypt private key
    ├── logging.py          # structlog JSON renderer + secret redactor
    ├── models/
    │   ├── __init__.py
    │   ├── account.py
    │   ├── candidate_leader.py
    │   ├── candidate_score_history.py
    │   ├── copy_template.py
    │   ├── follow_relation.py
    │   ├── leader_trade.py
    │   ├── my_trade.py
    │   ├── setting.py
    │   ├── backtest_job.py
    │   ├── pending_action.py
    │   ├── push_subscription.py
    │   └── event.py
    └── polymarket/
        ├── __init__.py
        └── client.py       # Async wrapper around py_clob_client_v2
```

## Import path

```python
from polyecho.shared.config import get_settings
from polyecho.shared.db import session_scope
from polyecho.shared.models import Account, CandidateLeader
from polyecho.shared.crypto import encrypt_private_key, decrypt_private_key
from polyecho.shared.polymarket.client import PolymarketClient
from polyecho.shared.logging import configure_logging
```

## Configuration

All settings are read from environment variables with the `POLYECHO_` prefix.
Copy `.env.example` to `.env` and fill in the values. See `config.py` for the
full list with descriptions.

Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `POLYECHO_DB_URL` | `postgresql+asyncpg://...` | Async Postgres URL |
| `POLYECHO_POLYMARKET_CLOB_URL` | `https://clob.polymarket.com` | CLOB API base URL |
| `POLYECHO_KEY_KDF_ITERATIONS` | `600000` | PBKDF2 iterations for key encryption |
| `POLYECHO_LOG_LEVEL` | `INFO` | Structlog minimum level |

## Running tests

```bash
# From repo root (uv workspace)
uv run pytest packages/shared/tests/ -v
```

Tests do not require a live Postgres connection.

## Datasource layer (`polyecho.shared.datasource`)

Phase 1 adds a read-only analytics abstraction over the Polymarket Data API.

### Quick start

```python
from polyecho.shared.datasource import get_datasource

ds = get_datasource()                          # FixtureDataSource (default)
leaders = await ds.get_leaderboard(limit=200)  # list[LeaderboardEntry]
trades  = await ds.get_user_activity("0x…")   # list[ActivityRecord]
pos     = await ds.get_positions("0x…")        # list[PositionRecord]
markets = await ds.get_markets_by_volume()     # list[MarketSummary]
holders = await ds.get_market_top_holders("0xcond_…")  # list[PositionRecord]
```

### Runtime selection

| `POLYECHO_DATASOURCE` | Implementation | Network |
|---|---|---|
| `fixture` (default) | `FixtureDataSource` | none — bundled JSON |
| `live` | `LiveDataSource` | Polymarket Data API |

`LiveDataSource` must be used as an async context manager:

```python
async with LiveDataSource() as ds:
    leaders = await ds.get_leaderboard()
```

### DataSource Protocol (binding contract for Wave 2)

```python
class DataSource(Protocol):
    async def get_leaderboard(
        self, *, category: str = "OVERALL", time_period: str = "MONTH",
        order_by: str = "PNL", limit: int = 200
    ) -> list[LeaderboardEntry]: ...

    async def get_user_activity(
        self, wallet: str, *, start: datetime | None = None,
        end: datetime | None = None, activity_type: str | None = "TRADE",
        limit: int | None = None
    ) -> list[ActivityRecord]: ...

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]: ...

    async def get_markets_by_volume(
        self, *, min_volume_24h: float = 100_000.0, limit: int = 200
    ) -> list[MarketSummary]: ...

    async def get_market_top_holders(
        self, condition_id: str, *, limit: int = 10
    ) -> list[PositionRecord]: ...
```

### DTOs

| Class | Key fields |
|---|---|
| `LeaderboardEntry` | `rank`, `wallet`, `username`, `pnl`, `volume`, `verified` |
| `ActivityRecord` | `wallet`, `activity_type`, `condition_id`, `side`, `size`, `price`, `usdc_size`, `timestamp`, `raw` |
| `PositionRecord` | `wallet`, `condition_id`, `asset`, `size`, `avg_price`, `current_value`, `cash_pnl`, `percent_pnl`, `title`, `outcome`, `raw` |
| `MarketSummary` | `condition_id`, `market_id`, `question`, `volume_24h`, `end_date`, `tokens`, `raw` |

All datetimes are tz-aware UTC. Wallet addresses are normalised to lowercase `0x…`.

### LiveDataSource reliability features

- Internal pagination: `get_leaderboard(limit=200)` makes 4 × 50-row pages automatically.
- Exponential-backoff retry: 3 attempts, base 1 s, cap 30 s.
- Circuit breaker: opens after 5 consecutive failures, resets after 60 s.

### TODO(verify)

`LiveDataSource.get_market_top_holders` raises `NotImplementedError` — the
per-market top-holders endpoint is not present in the verified Polymarket Data
API docs.  Use `FixtureDataSource` (the default) until this is confirmed.

### New config variable

| Variable | Default | Purpose |
|---|---|---|
| `POLYECHO_DATASOURCE` | `fixture` | Selects `FixtureDataSource` or `LiveDataSource` |

## Account derivation & signature-type detection (`polyecho.shared.accounts`)

Phase 3 adds wallet derivation + signature-type auto-detection (PRD §5.1).

```python
from polyecho.shared.accounts import derive_wallets, detect_signature_type

wallets = derive_wallets("0xEOA…")   # {0: eoa, 1: poly_proxy, 2: safe, 3: safe}
result  = await detect_signature_type("0xEOA…", datasource)  # DetectionResult
```

* `derive_wallets(eoa)` → the 4 candidate funder addresses keyed by signature
  type (0..3), all lowercased. Type 0 is the EOA itself; types 2 and 3
  intentionally share the Gnosis-Safe proxy address (the 1271 flag only
  changes signature *verification*, not the on-chain holder).
* `detect_signature_type(eoa, datasource, *, usdc_balance_fn=None)` queries
  the **frozen** `DataSource` (`get_positions` / `get_user_activity`, consumed
  as-is, never extended) for each candidate and ranks by
  **positions ≫ balance ≫ activity**. Returns a frozen `DetectionResult`
  (per-type `TypeEvidence`, `recommended_type/address`, `confidence`).
* `fetch_usdc_balance(address, *, client=None)` is an **optional** standalone
  best-effort `httpx` probe of the Polymarket data-api — deliberately *not*
  part of the `DataSource` protocol. Any failure → `None`.

> **TODO(verify):** The official docs confirm signature types 0/1/2 and that
> proxy wallets are *deterministic*, but do **not** publish the factory
> addresses / CREATE2 salt / proxy init-code hash; `py-clob-client-v2` ships
> no derivation helper. The factory/singleton constants in `accounts.py` are
> the community-known Polygon values, isolated as module constants and marked
> `# TODO(verify)`. Type 0 is always exact; detection ranks by observed
> on-chain evidence and does **not** require types 1/2/3 to be byte-exact.

## Security notes

- `encrypted_private_key` is written only by the CLI (`polyecho account import`).
  The web process (`dashboard-web`) must never be granted access to this column.
- The `crypto` module uses AES-256-GCM with PBKDF2-HMAC-SHA256 key derivation.
  A random 32-byte salt and 12-byte nonce are generated per encryption call.
- The structlog processor in `logging.py` automatically redacts any log field
  whose name contains `private_key`, `secret`, `passphrase`, or `signature`.
- `PolymarketClient` accepts the private key as a constructor argument.
  Clear it from memory as soon as the client is constructed.
