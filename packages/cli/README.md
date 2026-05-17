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

## `polyecho doctor`

The `doctor` command is the only fully-implemented command in Phase 0. It
runs four checks and renders a table:

| Check | Critical | What it tests |
|---|---|---|
| Config | yes | `get_settings()` loads; DB URL + Polymarket URLs present |
| Database | yes | Async `SELECT 1` via `polyecho.shared.db`; reports latency |
| Polymarket API | no | `GET <clob_url>/time`; reports HTTP status + latency |
| Tailnet | no | `tailscale status --json`; handles not-installed gracefully |

Exit code is **non-zero** when any critical check fails (Config or Database).
Polymarket API and Tailnet failures are warnings — the system can still start
without them.

### Behaviour when DB is down

```
PolyEcho — System Health Check

  Check             Status  Detail
  Config              ✓     DB driver=asyncpg  log_level=INFO
  Database            ✗     Connection failed: ...
  Polymarket API      ✓     GET https://clob.polymarket.com/time  HTTP 200  (210 ms)
  Tailnet             ✓     Running  hostname=shan-mac  dns=shan-mac.tail1234.ts.net

One or more critical checks failed.
```

Exit code: **1**

### Tailscale not installed (e.g. inside a container)

```
  Tailnet             ⚠     tailscale binary not found — not installed or not in PATH.
```

The command does **not** crash; it prints a warning and continues. If Config
and Database both pass, exit code is 0.

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
