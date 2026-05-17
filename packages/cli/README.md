# polyecho-cli

Command-line interface for the **PolyEcho** Polymarket copy-trading system.

## Installation (uv workspace)

```bash
# From the repo root — installs all workspace members including polyecho-cli
uv sync

# Or install just this package in editable mode
uv pip install -e packages/cli
```

## Usage

```
polyecho [COMMAND] [OPTIONS]
```

Run `polyecho --help` to see all commands.

## Command tree

```bash
# Account management (Phase 3)
polyecho account detect <eoa>
polyecho account import <eoa>
polyecho account list
polyecho account balance <name>

# Candidate pool discovery (Phase 1)
polyecho discovery run [--once]
polyecho discovery schedule
polyecho discovery promote <wallet>

# Backtest engine (Phase 2)
polyecho backtest new --leader <addr> --days <n>
polyecho backtest list
polyecho backtest show <id>

# Trader-core management (Phase 4)
polyecho trader start
polyecho trader status
polyecho trader pause-all
polyecho trader pause <leader_addr>

# Database operations
polyecho db migrate          # Phase 0
polyecho db backup           # Phase 1
polyecho db reconcile        # Phase 4

# Logs
polyecho logs <service>      # Phase 0

# System health (Phase 0 — fully implemented)
polyecho doctor
```

## `polyecho backtest`

Enqueue and inspect backtest jobs. The backtest worker (`python -m polyecho.backtest`) consumes pending jobs and writes results.

### `polyecho backtest new`

```
polyecho backtest new --leader <addr> [--leader <addr>]... [OPTIONS]

Options:
  --days INT            Historical days to simulate (default: 90)
  --capital FLOAT       Initial capital in USD (default: 1000.0)
  --copy-ratio FLOAT    Copy ratio, e.g. 0.001 = 0.1% (default: 0.001)
  --max-position FLOAT  Max position size in USD
  --min-trade FLOAT     Minimum trade size in USD
  --daily-loss FLOAT    Daily loss limit in USD
  --slippage-bps INT    Slippage in basis points (default: 150)
  --delay INT           Execution delay in seconds (default: 30)
  --category TEXT       Category whitelist entry (repeatable)
```

Inserts a row into `backtest_jobs` with `status='pending'` and prints the new job UUID.

### `polyecho backtest list`

```
polyecho backtest list [--limit 20]
```

Renders a Rich table of recent jobs: short ID, status, progress %, leaders, created at.

### `polyecho backtest show <id>`

```
polyecho backtest show <uuid-or-8-char-prefix>
```

Prints job params + status. If the job is completed and a `result` JSONB payload is present, renders:

- Per-leader stats table (return %, Sharpe, max drawdown, win rate, trade count)
- Three-scenario summary table (optimistic / neutral / pessimistic)

Exits with code 1 if the job is not found.

---

## `polyecho account` (Phase 3 — implemented)

Account management. Private keys are **CLI-only** and never touch the Web UI.

### `polyecho account detect <eoa>`

Derives the 4 candidate funder addresses for an EOA, queries the configured
datasource (positions/activity) for each, and prints a ranked Rich table plus
a recommended signature type + confidence. Read-only (no DB writes).

```
polyecho account detect 0xabc…
```

### `polyecho account import <eoa>`

```
polyecho account import <eoa> [--signature-type 0|1|2|3] [--name NAME]
```

Prompts (hidden) for the encryption **passphrase** (with confirmation) and
then the **private key**. The key is **never** a CLI argument and is never
echoed, logged, or printed. It is encrypted in-process with AES-256-GCM
(`shared.crypto`) and the `accounts` row is upserted with
`signature_type` / `funder_address` / `encrypted_private_key`, transitioning
`status` `pending_key_import` → `ready`. Exits non-zero on bad address /
empty key / DB error.

### `polyecho account list`

Renders a Rich table of all accounts (name, EOA, funder, sig type, status,
and whether an encrypted key is present). **Never** displays key material.

### `polyecho account balance <name>`

Resolves an account by name or EOA, sums open-position mark-to-market via the
datasource, and shows a best-effort USDC value (unavailable is reported
gracefully, not an error).

---

## `polyecho db migrate`

```
polyecho db migrate
```

Reads `infra/migrations/0000_init.sql` from the repo root and executes it against the database configured via `POLYECHO_DB_URL` (or the default `postgresql+asyncpg://polyecho:polyecho@localhost:5432/polyecho`).

- **Idempotent** — the SQL uses `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`.
- **Multi-statement** — the full script is passed to asyncpg's `exec_driver_sql` in a single call; asyncpg handles semicolon splitting internally.
- **Non-zero exit** on any DB or file error.

`db backup` and `db reconcile` remain stubs for Phase 1 / Phase 4.

---

## `polyecho doctor`

The `doctor` command is the only fully-implemented command in Phase 0. It
runs three checks and renders a table:

| Check | Critical | What it tests |
|---|---|---|
| Config | yes | `get_settings()` loads; DB URL + Polymarket URLs present |
| Database | yes | Async `SELECT 1` via `polyecho.shared.db`; reports latency |
| Polymarket API | no | `GET <clob_url>/time`; reports HTTP status + latency |

Exit code is **non-zero** when any critical check fails (Config or Database).
Polymarket API failures are warnings — the system can still start without it.

### Behaviour when DB is down

```
PolyEcho — System Health Check

  Check             Status  Detail
  Config              ✓     DB driver=asyncpg  log_level=INFO
  Database            ✗     Connection failed: ...
  Polymarket API      ✓     GET https://clob.polymarket.com/time  HTTP 200  (210 ms)

One or more critical checks failed.
```

Exit code: **1**

## Development

```bash
# Type-check
uv run mypy packages/cli/src

# Lint
uv run ruff check packages/cli/src

# Run doctor without installing
uv run python -m polyecho.cli doctor
```

## Short alias

Add to `~/.bashrc` or `~/.zshrc`:

```bash
alias pe='polyecho'
```
