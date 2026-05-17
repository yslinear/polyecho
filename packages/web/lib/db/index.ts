import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import { env } from "@/lib/env";
import * as schema from "./schema";

/**
 * postgres-js client — synchronous connection pool.
 * Drizzle uses this for all server-side queries in Next.js Server Components
 * and Server Actions. The connection URL comes from POLYECHO_DB_URL_DRIZZLE.
 */
const client = postgres(env.POLYECHO_DB_URL_DRIZZLE);

export const db = drizzle(client, { schema });

export type DB = typeof db;
