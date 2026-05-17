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
- **Tailwind CSS 3.4.x** + **shadcn/ui** (Phase 2 full component set — see below)
- **Recharts 2.x** via shadcn `chart` wrapper
- **@tanstack/react-table v8** via shadcn `data-table` wrapper
- **react-hook-form + zod + @hookform/resolvers** — backtest form
- **sonner** — toast notifications (Toaster wired in root layout)
- No auth — access restricted by Tailscale network isolation (PRD §5.9)

## shadcn/ui Component Set (Phase 2)

All source files live under `components/ui/`. Style: `default` / slate base-color, CSS variables, RSC-compatible.

Phase 0 (pre-existing): `button`, `card`, `badge`, `table`

Phase 2 additions (deps install in-container, no host install needed):

| Component | Radix dep |
|---|---|
| `input` | — |
| `label` | `@radix-ui/react-label` |
| `separator` | `@radix-ui/react-separator` |
| `skeleton` | — |
| `tooltip` | `@radix-ui/react-tooltip` |
| `select` | `@radix-ui/react-select` |
| `dialog` | `@radix-ui/react-dialog` |
| `sheet` | `@radix-ui/react-dialog` (reuse) |
| `tabs` | `@radix-ui/react-tabs` |
| `dropdown-menu` | `@radix-ui/react-dropdown-menu` |
| `form` | `react-hook-form`, `zod`, `@hookform/resolvers` |
| `sonner` | `sonner` |
| `chart` | `recharts` (already dep) |
| `sidebar` | collapsible icon/offcanvas, CSS-var theme |
| `data-table` | `@tanstack/react-table` |

## Phase 2 Pages

| Route | What changed |
|---|---|
| `app/layout.tsx` | Replaced flat NavSidebar with shadcn `SidebarProvider` + collapsible `AppSidebar` + global header (Pause All placeholder, Phase 4/5) + `Toaster` |
| `/discovery` | Rebuilt on shadcn `data-table`: column sorting, faceted filters (status / strategy / risk-flag), text filter, pagination |
| `/discovery/[wallet]` | shadcn `Tabs` (Overview / Stats / Score History); score chart migrated to shadcn `chart` (Recharts-based) |
| `/backtest/new` | Real shadcn `form` (react-hook-form + zod); Server Action inserts `backtest_jobs` row (status=queued), toasts, redirects |
| `/backtest/[id]` | Reads job row; progress bar while running; PnL curve + stats table + 3-scenario side-by-side when completed |

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
