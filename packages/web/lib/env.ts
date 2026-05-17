/**
 * Typed environment variable accessor for @polyecho/web.
 * All required vars are validated at module load time so the process
 * fails fast with a clear error rather than a silent undefined later.
 */

function requireEnv(key: string): string {
  const value = process.env[key];
  if (!value) {
    throw new Error(
      `Missing required environment variable: ${key}\n` +
        `Set it in your .env.local or docker-compose environment block.`
    );
  }
  return value;
}

export const env = {
  /**
   * Synchronous postgres-js connection URL used by Drizzle ORM.
   * Example: postgres://polyecho:secret@localhost:5432/polyecho
   */
  POLYECHO_DB_URL_DRIZZLE: requireEnv("POLYECHO_DB_URL_DRIZZLE"),
} as const;
