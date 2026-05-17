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

## Security notes

- `encrypted_private_key` is written only by the CLI (`polyecho account import`).
  The web process (`dashboard-web`) must never be granted access to this column.
- The `crypto` module uses AES-256-GCM with PBKDF2-HMAC-SHA256 key derivation.
  A random 32-byte salt and 12-byte nonce are generated per encryption call.
- The structlog processor in `logging.py` automatically redacts any log field
  whose name contains `private_key`, `secret`, `passphrase`, or `signature`.
- `PolymarketClient` accepts the private key as a constructor argument.
  Clear it from memory as soon as the client is constructed.
