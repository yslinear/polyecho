# @polyecho/web — Dashboard

Next.js 15 App Router dashboard for the PolyEcho copy-trading system.

## Running via Docker Compose (recommended)

The dashboard is orchestrated by `infra/docker-compose.yml`. Do **not** install
dependencies on the host — the container handles it.

```bash
# From repo root
docker compose -f infra/docker-compose.yml up web
```

The Next.js server runs with `output: 'standalone'` and is started inside the
container via:

```bash
node .next/standalone/server.js
```

Tailscale Serve proxies HTTPS on port 443 → container port 3000. See
`infra/tailscale/` for ACL and Serve config.

## Environment Variables

Set in `infra/docker-compose.yml` or a `.env` file mounted into the container:

| Variable | Description |
|---|---|
| `POLYECHO_DB_URL_DRIZZLE` | Postgres URL for Drizzle ORM (sync, postgres-js). e.g. `postgres://polyecho:secret@db:5432/polyecho` |

## Database Migrations

Migrations are generated into `infra/migrations/` (owned by the infra layer)
and applied by `drizzle-kit`:

```bash
# From packages/web/ inside the container or via docker compose run
pnpm db:generate   # drizzle-kit generate
pnpm db:migrate    # drizzle-kit migrate
```

Or via the Python CLI (delegates to drizzle-kit):

```bash
polyecho db migrate
```

## Stack

- **Next.js 15.1.8** App Router, Server Components, TypeScript
- **Drizzle ORM 0.39.x** with postgres-js driver
- **Tailwind CSS 3.4.x** + **shadcn/ui** primitives (button, card, table, badge)
- **Recharts 2.x** (charts — wired up in Phase 2/3)
- No auth — access restricted by Tailscale network isolation (PRD §5.9)

## Development (host, optional)

If you must run locally without Docker (corepack + pnpm required):

```bash
cd packages/web
corepack enable
pnpm install          # one-time
cp ../../.env.example .env.local   # set POLYECHO_DB_URL_DRIZZLE
pnpm dev
```

> The canonical workflow is docker compose. Host installs are not required.

## PWA / Web Push

`public/manifest.json` is present for the PWA shell. Service Worker and VAPID
Web Push are **Phase 5** — not implemented. To receive push notifications on
iPhone Safari you must first add the Dashboard to Home Screen as a PWA (requires
Safari 16.4+).
