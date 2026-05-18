import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import { env } from "@/lib/env";
import * as schema from "./schema";

/**
 * postgres-js client — lazily-connecting connection pool.
 *
 * Drizzle uses this for all server-side queries in Next.js Server Components
 * and Server Actions. The connection URL comes from POLYECHO_DB_URL_DRIZZLE.
 *
 * rev-web R17 MEDIUM — explicit, documented options (the client was
 * previously created with ZERO options):
 *
 *  - `max: 10` — a bounded pool. The dashboard is a single-operator,
 *    localhost-only UI; Next.js Server Components/Actions issue short
 *    queries. An UNBOUNDED pool (postgres-js default is 10, but we pin it
 *    explicitly so a Next dev/prod worker-count change can never silently
 *    fan out and exhaust Postgres `max_connections` against the small
 *    docker-compose Postgres). 10 is ample for one operator and is the
 *    library's own sane default — pinned, not changed.
 *  - `idle_timeout: 30` — reap idle sockets after 30s so a long-lived Next
 *    server process does not pin connections it is not using.
 *  - `ssl: false` — EXPLICIT. This deployment is the docker-compose stack:
 *    `web` talks to the `postgres` service over the project-internal Docker
 *    network (or `localhost` in dev), never the public internet, and the
 *    bundled `postgres:16` image is not configured for TLS. Leaving `ssl`
 *    unset makes postgres-js infer from the URL (ambiguous: a future
 *    `?sslmode=` in the URL would flip behaviour). We pin `false` so the
 *    local-docker transport is unambiguous and never silently attempts a
 *    TLS handshake the local server would reject. If this is ever pointed
 *    at a managed/remote Postgres, this MUST be revisited to `require`.
 */
const client = postgres(env.POLYECHO_DB_URL_DRIZZLE, {
  max: 10,
  idle_timeout: 30,
  ssl: false,
});

export const db = drizzle(client, { schema });

export type DB = typeof db;
