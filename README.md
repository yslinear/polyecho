# PolyEcho

PolyEcho is a personal Polymarket copy-trading system: discover quality leaders,
backtest them with slippage/latency models, and copy-trade them with full risk
controls — all self-hosted, with private keys that never leave the local machine.

Full specification: [`docs/PRD.md`](docs/PRD.md).

## Monorepo layout

| Path                 | Component       | Description                                  |
| -------------------- | --------------- | -------------------------------------------- |
| `packages/shared`    | `polyecho.shared`    | Polymarket client, ORM models, db session, health probes |
| `packages/core`      | `polyecho.core`      | trader-core — holds keys, signs orders    |
| `packages/discovery` | `polyecho.discovery` | leader discovery + scoring worker         |
| `packages/backtest`  | `polyecho.backtest`  | slippage/latency backtest engine          |
| `packages/push`      | `polyecho.push`      | web-push-sender (VAPID)                    |
| `packages/cli`       | `polyecho.cli`       | `polyecho` command-line tool              |
| `packages/web`       | `@polyecho/web`      | Next.js 15 dashboard (Drizzle ORM)        |
| `infra/`             | —               | docker-compose, Dockerfiles, migrations      |

Python packages share the implicit-namespace package `polyecho` (PEP 420) and are
managed as a **uv workspace**. The web app is a separate **pnpm workspace**.

## How it works

```
discovery ──► leader candidate pool ──► (you review in web / CLI)
                     │
                     ▼
              backtest a leader  ──► slippage/latency-modelled PnL
                     │
                     ▼
   trader-core copies a followed leader's fills ──► risk-controlled orders
                     │
   web dashboard (read-mostly)  +  web-push alerts on every event
```

Every service talks **only through Postgres** — no service opens a port to
another. The private key lives **only** in `trader-core`; control flows by
writing rows to the `settings` table (via the CLI or the web `/settings`
page), never by an RPC into the key-holding process.

## Prerequisites

- **Docker Desktop** (macOS/Windows/Linux). That is the only requirement —
  nothing (uv, pnpm, Python, Node, Postgres) is installed on the host. The
  repo is designed to run entirely inside containers.

## Quick start

```bash
# 1. Configuration — copy the template and edit as needed (see below).
cp .env.example .env

# 2. Bring up the full stack (Postgres + one-shot migrate + all workers + web).
#    A fresh DB volume is migrated automatically before any worker starts.
docker compose -f infra/docker-compose.yml up -d

# 3. Health check — probes config, DB, Polymarket endpoints, RPC, proxy/VPN.
docker compose -f infra/docker-compose.yml run --rm cli polyecho doctor

# 4. Open the dashboard.
open http://127.0.0.1:3000
```

Stop / inspect:

```bash
docker compose -f infra/docker-compose.yml ps              # service status
docker compose -f infra/docker-compose.yml logs -f core    # follow a service
docker compose -f infra/docker-compose.yml down            # stop (keep data)
docker compose -f infra/docker-compose.yml down -v         # stop + wipe DB
```

## Configuration (`.env`)

`.env` is git-ignored and **never committed**. Every variable is prefixed
`POLYECHO_`. Defaults below are what the stack uses when the variable is unset.

| Variable | Default | Purpose |
| --- | --- | --- |
| `POLYECHO_DB_URL` | `…@db:5432/polyecho` | Async (asyncpg) DB URL for Python services. In-container the host is `db`. |
| `POLYECHO_DB_URL_DRIZZLE` | `…@db:5432/polyecho` | Sync DB URL the web app's Drizzle ORM uses. |
| `POLYECHO_DATASOURCE` | `fixture` | `fixture` = offline canned data (safe default). `live` = real Polymarket APIs. |
| `POLYECHO_POLYMARKET_CLOB_URL` | `https://clob.polymarket.com` | CLOB order API base. |
| `POLYECHO_POLYMARKET_DATA_API_URL` | `https://data-api.polymarket.com` | Leaderboard / activity / positions API base. |
| `POLYECHO_POLYGON_RPC_URL` | `https://polygon-rpc.com` | Polygon RPC (on-chain redeem / balances). |
| `POLYECHO_POLYMARKET_CHAIN_ID` | `137` | Polygon mainnet. |
| `POLYECHO_POLYMARKET_PROXY_URL` | _(blank = direct)_ | Outbound proxy for **all** Polymarket calls (geo-block workaround). The web `/settings` page writes a `settings` DB row that overrides this at runtime. |
| `POLYECHO_KEY_KDF_ITERATIONS` | `600000` | PBKDF2 rounds for the encrypted account key. Passphrase is entered interactively, never stored. |
| `POLYECHO_VAPID_PUBLIC_KEY` / `_PRIVATE_KEY` / `_SUBJECT` | _(blank)_ | Web-push (VAPID, RFC 8292). Generate with `polyecho push vapid-keygen`. |
| `POLYECHO_LIVE_TRADING` | _(unset = false)_ | **MASTER live-money lock.** While false, every real order / redeem RAISES at the single chokepoint. See [Going live](#going-live-real-money). |
| `POLYECHO_AUTO_REDEEM` | _(unset = false)_ | Auto claim-winnings. Only acts when this **and** `POLYECHO_LIVE_TRADING` are both true. |
| `POLYECHO_WEB_PORT` | `3000` | Dashboard port (bound to `127.0.0.1` only). |
| `POLYECHO_LOG_LEVEL` | `INFO` | Structured-log level. |
| `POLYECHO_TZ` | `Asia/Taipei` | Display timezone (used for "today" PnL boundaries). |

> **Proxy / VPN note.** If Polymarket is geo-blocked, run your own VPN/proxy
> as a **separate** concern (it is the operator's responsibility, out of
> project scope) and point `POLYECHO_POLYMARKET_PROXY_URL` at it — e.g.
> `http://host.docker.internal:8888` for a proxy running on the host. Do not
> add the proxy to the tracked compose file; keep it in your private `.env`.

## Services

| Service | Ports | Role |
| --- | --- | --- |
| `db` | `127.0.0.1:5432` | PostgreSQL 16. Localhost-bound only. |
| `migrate` | — | One-shot. Applies `infra/migrations` to a healthy DB, then exits. Every app waits for it. |
| `core` | **none** | trader-core. Holds the private key, signs orders. Opens no port (key isolation). |
| `discovery` | none | Leader discovery + scoring worker. |
| `backtest` | none | Polls `backtest_jobs`, runs the engine, writes results. |
| `push` | none | Web-push sender (outbound VAPID only). |
| `web` | `127.0.0.1:3000` | Next.js 15 dashboard. Read-mostly, **no auth by design**. |
| `cli` | — | On-demand (`profile: tools`). The `polyecho` command. |

## CLI reference

The `polyecho` CLI is the operator control surface. Canonical invocation
(runs a one-shot container against the running stack's DB):

```bash
docker compose -f infra/docker-compose.yml run --rm cli polyecho <command> [args]
```

`polyecho --help` and `polyecho <group> --help` print full usage for any
group. Command groups and their commands:

### `polyecho doctor`
End-to-end health check: validates config, DB connectivity, Polymarket CLOB /
Data API / Polygon RPC reachability, and proxy/VPN egress. Run this first
whenever something looks wrong.

### `polyecho account` — trading account & key management (CLI-only)
| Command | What it does |
| --- | --- |
| `set-danger-passphrase` | Set the passphrase that encrypts the stored key. |
| `detect` | Detect/derive the account address from a key. |
| `import` | Import a private key (encrypted at rest; **CLI-only**, never the web). |
| `list` | List configured accounts. |
| `balance` | Show on-chain / Polymarket balances for an account. |

### `polyecho discovery` — leader candidate pool
| Command | What it does |
| --- | --- |
| `run` | Run one discovery + scoring sweep now. |
| `schedule` | Run discovery on the recurring schedule. |
| `promote` | Promote a candidate into the followed-leaders set. |

### `polyecho backtest` — slippage/latency backtest engine
| Command | What it does |
| --- | --- |
| `new` | Queue a new backtest job (by leader + scenario params). |
| `list` | List backtest jobs and their status. |
| `show` | Show a completed backtest's results. |

### `polyecho trader` — trader-core controls (DB-driven; never holds keys here)
| Command | What it does |
| --- | --- |
| `status` | Current trader-core state, followed leaders, open risk. |
| `start` | Start copy-trading a followed leader. |
| `pause` / `resume` | Pause/resume a single leader. |
| `pause-all` / `resume-all` | Global kill-switch / restore. |
| `enable-live` / `disable-live` | Toggle the live-money lock (see below). |
| `skips` | Inspect skipped copy decisions and why. |
| `redeems` | Inspect claim-winnings / redeem activity. |

### `polyecho db` — database operations
| Command | What it does |
| --- | --- |
| `migrate` | Apply migrations (the stack runs this automatically on startup). |
| `backup` | Dump the database. |
| `reconcile` | Reconcile DB state against on-chain / Polymarket truth. |

### `polyecho logs show`
Tail structured service logs.

### `polyecho push vapid-keygen`
Generate a VAPID public/private key pair for web-push; paste the values into
`.env` (`POLYECHO_VAPID_*`).

### `polyecho alerts list`
List recent push events (the events log the dashboard surfaces).

## Typical workflows

**Discover and review leaders**

```bash
docker compose -f infra/docker-compose.yml run --rm cli polyecho discovery run
# review candidates at http://127.0.0.1:3000/discovery
docker compose -f infra/docker-compose.yml run --rm cli polyecho discovery promote <wallet>
```

**Backtest a leader before following**

```bash
docker compose -f infra/docker-compose.yml run --rm cli polyecho backtest new --help
docker compose -f infra/docker-compose.yml run --rm cli polyecho backtest list
docker compose -f infra/docker-compose.yml run --rm cli polyecho backtest show <id>
```

**Import the trading key (CLI-only, encrypted at rest)**

```bash
docker compose -f infra/docker-compose.yml run --rm cli polyecho account set-danger-passphrase
docker compose -f infra/docker-compose.yml run --rm cli polyecho account import
```

**Enable web-push alerts**

```bash
docker compose -f infra/docker-compose.yml run --rm cli polyecho push vapid-keygen
# put the keys in .env, then: docker compose ... up -d --force-recreate push web
```

### Going live (real money)

Live trading is **hard-locked**. With `POLYECHO_LIVE_TRADING` unset/false
(the default), every CLOB order and on-chain redeem raises at a single
chokepoint — the system runs fully in dry-run/fixture mode and **moves no
real funds**. To go live you must, deliberately:

1. Fund and import the account (`polyecho account import`).
2. Set `POLYECHO_DATASOURCE=live` and `POLYECHO_LIVE_TRADING=true` in `.env`.
3. Recreate the stack and arm trader-core:
   ```bash
   docker compose -f infra/docker-compose.yml up -d --force-recreate
   docker compose -f infra/docker-compose.yml run --rm cli polyecho trader enable-live
   docker compose -f infra/docker-compose.yml run --rm cli polyecho trader start <leader>
   ```

`polyecho trader pause-all` is the instant global kill-switch. `disable-live`
re-engages the lock.

## Web dashboard

`http://127.0.0.1:3000` — bound to localhost only, **no authentication by
design** (it is never exposed to a network; remote access is the operator's
responsibility and out of scope). It is read-mostly: overview, discovery,
leader detail + score charts, backtests, trades, positions, accounts,
leaders, a `/status` integration-health page, and a `/settings` page that
writes the Polymarket proxy URL into the `settings` table.

## Security model (non-negotiable)

- `trader-core` holds private keys and opens **no** network port.
- The private key **never** enters any web process; key import is **CLI-only**.
- All control flows through DB `settings` changes, not RPC into the key-holder.
- Live trading is gated by one chokepoint (`assert_live_allowed`); default is
  dry-run/fixture — no real funds move unless explicitly unlocked.
- `db` and `web` bind to `127.0.0.1` only; there is no auth layer by design.

## Troubleshooting

- **Anything failing?** Run `polyecho doctor` first — it pinpoints config,
  DB, API, RPC and proxy/VPN problems.
- **Fresh/empty DB:** the one-shot `migrate` service populates the schema
  automatically before workers start; no manual step needed.
- **Geo-blocked Polymarket calls:** set `POLYECHO_POLYMARKET_PROXY_URL` (or
  the web `/settings` proxy field) — see the proxy/VPN note above.
- **Reset everything:** `docker compose -f infra/docker-compose.yml down -v`
  wipes the DB volume; the next `up` re-migrates from scratch.
