# Database Migrations

PolyEcho uses a two-track approach:

1. **Phase 1 baseline** — `0000_init.sql` is a hand-authored, reviewable SQL
   file that creates all **14** core tables and performance indexes.  It is
   idempotent (`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`).
2. **Ongoing changes** — after the baseline, use **Drizzle ORM** (`drizzle-kit`)
   to generate incremental migration files; never hand-edit those generated files.

---

## Applying the Phase 1 Baseline

### Via `scripts/dev.sh` (recommended during development)

```bash
bash scripts/dev.sh up       # ensure DB is running
bash scripts/dev.sh migrate  # apply 0000_init.sql
```

### Directly against the running DB container

```bash
docker compose -f infra/docker-compose.yml exec db \
  psql -U polyecho -d polyecho \
  -f /dev/stdin < infra/migrations/0000_init.sql
```

Or copy the file into the container first:

```bash
docker compose -f infra/docker-compose.yml cp \
  infra/migrations/0000_init.sql db:/tmp/0000_init.sql

docker compose -f infra/docker-compose.yml exec db \
  psql -U polyecho -d polyecho -f /tmp/0000_init.sql
```

### Automatically, via the compose `migrate` one-shot service (recommended)

`infra/docker-compose.yml` ships a dedicated **`migrate`** service: it builds
`Dockerfile.cli`, runs `polyecho db migrate` once against a *healthy* `db`,
then exits 0. Every app service (`core`, `discovery`, `push`, `backtest`,
`web`) declares `depends_on: { migrate: { condition:
service_completed_successfully } }`, so a plain:

```bash
docker compose -f infra/docker-compose.yml up -d
```

on a **fresh volume** populates the full 14-table schema *before* any worker
connects — the stack is self-sufficient (no manual migrate step required).
Ordering is: `db` healthy → `migrate` completed-successfully → apps start.

### Manually, via the CLI container (matches `polyecho db migrate`)

```bash
docker compose -f infra/docker-compose.yml run --rm cli \
  polyecho db migrate --yes

# Inspect the resolved (password-redacted) target without executing:
docker compose -f infra/docker-compose.yml run --rm cli \
  polyecho db migrate --dry-run
```

---

## Generating Future Migrations (Drizzle-kit)

After editing `packages/web/lib/db/schema.ts`:

```bash
# From repo root
pnpm db:generate
# or directly:
pnpm --filter @polyecho/web db:generate
```

The Drizzle config in `packages/web/drizzle.config.ts` writes output to this
directory (`out: '../../infra/migrations'`).

## Applying Drizzle-generated Migrations

```bash
# From repo root (requires DB to be running)
pnpm db:migrate
# or via Docker:
docker compose -f infra/docker-compose.yml run --rm --profile tools \
  cli polyecho db migrate
```

---

## Directory Contents

| Path | Description |
|------|-------------|
| `0000_init.sql` | Phase 1 baseline — all 14 tables + indexes |
| `*.sql` | Drizzle-generated incremental migrations (committed to git) |
| `meta/` | Drizzle internal snapshot directory (committed to git) |
| `.gitkeep` | Ensures directory exists before the first migration |
| `README.md` | This file |

## Notes

- `0000_init.sql` is safe to re-run (idempotent); subsequent Drizzle files are not — apply them only once via `pnpm db:migrate`.
- Never hand-edit Drizzle-generated `.sql` files.
- The Postgres DB name, user, and password are all `polyecho` (see `.env.example`).
- Connection URL for Drizzle uses the sync driver: `POLYECHO_DB_URL_DRIZZLE`.
