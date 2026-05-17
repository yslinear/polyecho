# PolyEcho

> 「跟單，是延遲的回聲。」

PolyEcho is a personal Polymarket copy-trading system: discover quality leaders,
backtest them with slippage/latency models, and copy-trade them with full risk
controls — all self-hosted, with private keys that never leave the local machine.

Full specification: [`docs/PRD.md`](docs/PRD.md).

## Monorepo layout

| Path                 | Component       | Description                                  |
| -------------------- | --------------- | -------------------------------------------- |
| `packages/shared`    | `polyecho.shared`    | Polymarket client, ORM models, db session |
| `packages/core`      | `polyecho.core`      | trader-core — holds keys, signs orders    |
| `packages/discovery` | `polyecho.discovery` | leader discovery + scoring worker         |
| `packages/backtest`  | `polyecho.backtest`  | slippage/latency backtest engine          |
| `packages/push`      | `polyecho.push`      | web-push-sender (VAPID)                    |
| `packages/cli`       | `polyecho.cli`       | `polyecho` command-line tool              |
| `packages/web`       | `@polyecho/web`      | Next.js 15 dashboard (Drizzle ORM)        |
| `infra/`             | —               | docker-compose, Dockerfiles, migrations      |

Python packages share the implicit-namespace package `polyecho` (PEP 420) and are
managed as a **uv workspace**. The web app is a separate **pnpm workspace**.

## Quick start (development)

All tooling runs **inside containers** — nothing is installed on the host
(the repo lives in iCloud Drive; the host stays clean).

```bash
# Full stack: Postgres + all services
docker compose -f infra/docker-compose.yml up -d

# Dev shell — uv / pnpm run *inside* the dev container
docker compose -f infra/docker-compose.dev.yml run --rm dev

# Health check
docker compose -f infra/docker-compose.yml run --rm cli polyecho doctor
```

## Status

Phase 0 (infrastructure scaffolding). See PRD §9 for the roadmap.

## Security model (non-negotiable)

- `trader-core` holds private keys and opens **no** network port.
- The private key **never** enters any web process.
- All control flows through DB setting changes.
- The Web UI binds to `127.0.0.1` only and has **no auth layer by design** — it is
  never exposed to the network. Private/remote access (VPN, tunnel, etc.) is the
  **operator's responsibility, out of project scope**.
- Private-key import is **CLI-only**.
