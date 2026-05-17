import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * setRiskMode + setGlobalPaused server actions (PRD §5.6 / §5.8).
 *
 * Contract: BOTH upsert a single `settings` row as a BARE STRING value
 * (identical convention to polymarket_proxy_url), keyed exactly:
 *   - risk_mode     -> "normal" | "conservative"  (Python reads this live)
 *   - global_paused -> "true" | "false"
 *
 * Drizzle is fully mocked. No DB, no network.
 */

const onConflictDoUpdate = vi.fn().mockResolvedValue(undefined);
const insertValues = vi.fn(() => ({ onConflictDoUpdate }));
const insert = vi.fn(() => ({ values: insertValues }));
const deleteWhere = vi.fn().mockResolvedValue(undefined);
const del = vi.fn(() => ({ where: deleteWhere }));

vi.mock("@/lib/db", () => ({
  db: {
    insert: (...a: unknown[]) => insert(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import { setRiskMode, setGlobalPaused } from "@/app/settings/actions";

beforeEach(() => {
  insert.mockClear();
  insertValues.mockClear();
  onConflictDoUpdate.mockClear().mockResolvedValue(undefined);
});

describe("setRiskMode", () => {
  it("upserts the bare string 'conservative' under key risk_mode", async () => {
    const res = await setRiskMode("conservative");
    expect(res.ok).toBe(true);
    expect(res.value).toBe("conservative");
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.key).toBe("risk_mode");
    expect(row.value).toBe("conservative");
    expect(typeof row.value).toBe("string"); // bare string, NOT {value:...}
    const conflict = onConflictDoUpdate.mock.calls[0][0] as {
      set: Record<string, unknown>;
    };
    expect(conflict.set.value).toBe("conservative");
  });

  it("upserts 'normal' under key risk_mode", async () => {
    const res = await setRiskMode("normal");
    expect(res.value).toBe("normal");
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.key).toBe("risk_mode");
    expect(row.value).toBe("normal");
  });

  it("coerces any unknown input to the safe 'normal'", async () => {
    const res = await setRiskMode("bogus" as unknown as "normal");
    expect(res.value).toBe("normal");
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("normal");
  });
});

describe("setGlobalPaused", () => {
  it("upserts the bare string 'true' under key global_paused", async () => {
    const res = await setGlobalPaused(true);
    expect(res.ok).toBe(true);
    expect(res.paused).toBe(true);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.key).toBe("global_paused");
    expect(row.value).toBe("true");
    expect(typeof row.value).toBe("string");
  });

  it("upserts 'false' when resuming", async () => {
    const res = await setGlobalPaused(false);
    expect(res.paused).toBe(false);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("false");
  });
});
