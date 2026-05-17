import type { Config } from "drizzle-kit";

export default {
  schema: "./lib/db/schema.ts",
  // PRD §4.4: migrations are owned by infra/, not packages/web/
  out: "../../infra/migrations",
  dialect: "postgresql",
  dbCredentials: {
    url: process.env.POLYECHO_DB_URL_DRIZZLE!,
  },
} satisfies Config;
