import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * setRiskMode + setGlobalPaused server actions (PRD §5.6 / §5.8).
 *
 * Contract: BOTH upsert a single `settings` row as a BARE STRING value
 * (identical convention to polymarket_proxy_url), keyed exactly:
 *   - risk_mode     -> "normal" | "conservative"  (Python reads this live)
 *   - global_paused -> "true" | "false"
 *
 * P7d / R24: the risk-increasing direction must also pass an inline-confirm
 * gate BEFORE the upsert:
 *   - setRiskMode("normal")          → CONFIRM_PHRASE only
 *   - setGlobalPaused(false)         → CONFIRM_PHRASE + danger passphrase
 *   - safe-direction (conservative / true) stays bare two-arg / one-arg
 *
 * Drizzle + the passphrase verify path are mocked: no DB, no PBKDF2.
 */

// Mutable rows for the danger_passphrase_hash row read by the gate. Returned
// as a Promise from .limit(1) — the gate then reads `.value` off [0].
let hashRows: { value: string }[] = [];

const onConflictDoUpdate = vi.fn().mockResolvedValue(undefined);
const insertValues = vi.fn(() => ({ onConflictDoUpdate }));
const insertReturning = vi.fn().mockResolvedValue([{ id: 1 }]);
const insert = vi.fn(() => ({
  values: (...args: unknown[]) => {
    insertValues(...args);
    return { onConflictDoUpdate, returning: insertReturning };
  },
}));
const deleteWhere = vi.fn().mockResolvedValue(undefined);
const del = vi.fn(() => ({ where: deleteWhere }));
const selectLimit = vi.fn(() => Promise.resolve(hashRows));
const selectWhere = vi.fn(() => ({ limit: selectLimit }));
const selectFrom = vi.fn(() => ({ where: selectWhere }));
const select = vi.fn(() => ({ from: selectFrom }));

vi.mock("@/lib/db", () => ({
  db: {
    insert: (...a: unknown[]) => insert(...a),
    delete: (...a: unknown[]) => del(...a),
    select: (...a: unknown[]) => select(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

// Mock the passphrase verifier so the gate is exercised without PBKDF2.
const verifyPassphrase = vi.fn(async (_pw: string, _stored: string) => true);
vi.mock("@/lib/passphrase", () => ({
  verifyPassphrase: (...a: unknown[]) => verifyPassphrase(...a),
}));

import { setRiskMode, setGlobalPaused } from "@/app/settings/actions";

const CONFIRM_PHRASE = "確認執行";

beforeEach(() => {
  insert.mockClear();
  insertValues.mockClear();
  onConflictDoUpdate.mockClear().mockResolvedValue(undefined);
  insertReturning.mockClear().mockResolvedValue([{ id: 1 }]);
  select.mockClear();
  selectFrom.mockClear();
  selectWhere.mockClear();
  selectLimit.mockClear();
  verifyPassphrase.mockReset().mockImplementation(async () => true);
  hashRows = [{ value: "pbkdf2$600000$YWFh$YmJi" }];
});

describe("setRiskMode", () => {
  it("upserts the bare string 'conservative' under key risk_mode (safe-direction, no gate)", async () => {
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

  it("upserts 'normal' under key risk_mode when the confirm phrase matches", async () => {
    const res = await setRiskMode("normal", { confirmText: CONFIRM_PHRASE });
    expect(res.ok).toBe(true);
    expect(res.value).toBe("normal");
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.key).toBe("risk_mode");
    expect(row.value).toBe("normal");
  });

  it("rejects 'normal' without the confirm phrase and writes nothing", async () => {
    const res = await setRiskMode("normal");
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/確認執行/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects 'normal' when the confirm phrase is wrong and writes nothing", async () => {
    const res = await setRiskMode("normal", { confirmText: "wrong" });
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });

  it("coerces any unknown input to the safe 'normal' but still gates it", async () => {
    const res = await setRiskMode(
      "bogus" as unknown as "normal",
      { confirmText: CONFIRM_PHRASE }
    );
    expect(res.value).toBe("normal");
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("normal");
  });
});

describe("setGlobalPaused", () => {
  it("upserts the bare string 'true' under key global_paused (safe-direction, no gate)", async () => {
    const res = await setGlobalPaused(true);
    expect(res.ok).toBe(true);
    expect(res.paused).toBe(true);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.key).toBe("global_paused");
    expect(row.value).toBe("true");
    expect(typeof row.value).toBe("string");
  });

  it("upserts 'false' when resuming with valid confirm + passphrase", async () => {
    const res = await setGlobalPaused(false, {
      confirmText: CONFIRM_PHRASE,
      passphrase: "operator-secret",
    });
    expect(res.ok).toBe(true);
    expect(res.paused).toBe(false);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("false");
    expect(verifyPassphrase).toHaveBeenCalledTimes(1);
  });

  it("rejects resume without the confirm phrase (writes nothing)", async () => {
    const res = await setGlobalPaused(false, {
      passphrase: "operator-secret",
    });
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/確認執行/);
    expect(insert).not.toHaveBeenCalled();
    expect(verifyPassphrase).not.toHaveBeenCalled();
  });

  it("rejects resume when the passphrase is missing (writes nothing)", async () => {
    const res = await setGlobalPaused(false, {
      confirmText: CONFIRM_PHRASE,
    });
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/通行碼/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects resume when danger_passphrase_hash is not provisioned", async () => {
    hashRows = []; // no row -> fail-closed
    const res = await setGlobalPaused(false, {
      confirmText: CONFIRM_PHRASE,
      passphrase: "secret",
    });
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/set-danger-passphrase/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects resume when verifyPassphrase reports a constant-time mismatch", async () => {
    verifyPassphrase.mockImplementationOnce(async () => false);
    const res = await setGlobalPaused(false, {
      confirmText: CONFIRM_PHRASE,
      passphrase: "wrong",
    });
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/通行碼錯誤/);
    expect(insert).not.toHaveBeenCalled();
  });
});
