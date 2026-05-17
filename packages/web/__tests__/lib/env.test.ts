import { describe, it, expect, beforeEach, afterEach } from "vitest";

/**
 * lib/env validates required vars at module load time. We use isolated
 * dynamic imports with a manipulated process.env so each case re-evaluates
 * the module top-level code.
 */
describe("lib/env", () => {
  const ORIGINAL = process.env.POLYECHO_DB_URL_DRIZZLE;

  beforeEach(() => {
    delete process.env.POLYECHO_DB_URL_DRIZZLE;
  });

  afterEach(() => {
    if (ORIGINAL === undefined) {
      delete process.env.POLYECHO_DB_URL_DRIZZLE;
    } else {
      process.env.POLYECHO_DB_URL_DRIZZLE = ORIGINAL;
    }
  });

  it("throws a descriptive error when POLYECHO_DB_URL_DRIZZLE is unset", async () => {
    await expect(
      import(`@/lib/env?case=missing-${Date.now()}`)
    ).rejects.toThrow(/Missing required environment variable: POLYECHO_DB_URL_DRIZZLE/);
  });

  it("throws when the var is set but empty", async () => {
    process.env.POLYECHO_DB_URL_DRIZZLE = "";
    await expect(
      import(`@/lib/env?case=empty-${Date.now()}`)
    ).rejects.toThrow(/Missing required environment variable/);
  });

  it("exposes the value when the var is present", async () => {
    process.env.POLYECHO_DB_URL_DRIZZLE =
      "postgres://polyecho:secret@localhost:5432/polyecho";
    const mod = await import(`@/lib/env?case=ok-${Date.now()}`);
    expect(mod.env.POLYECHO_DB_URL_DRIZZLE).toBe(
      "postgres://polyecho:secret@localhost:5432/polyecho"
    );
  });
});
