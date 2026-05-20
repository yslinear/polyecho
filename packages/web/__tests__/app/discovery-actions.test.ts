import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * Discovery Server Actions — rev-web I-2 server-side validation + P7d gate.
 *
 *  - updateLeaderStatus: allowlist of status; .returning() existence check.
 *  - "following" (risk-increasing promote) must pass the inline-confirm gate
 *    (CONFIRM_PHRASE only) BEFORE the UPDATE — P7d / R24.
 *  - "watching" / "rejected" are safe-direction (single-click, no gate).
 *
 * Drizzle (`@/lib/db`) is fully mocked: no DB, no network.
 */

const updReturning = vi.fn().mockResolvedValue([{ wallet: "0xabc" }]);
const updWhere = vi.fn(() => ({ returning: updReturning }));
const updSet = vi.fn(() => ({ where: updWhere }));
const update = vi.fn(() => ({ set: updSet }));

const insReturning = vi.fn().mockResolvedValue([{ id: "job-1" }]);
const insValues = vi.fn(() => ({ returning: insReturning }));
const insert = vi.fn(() => ({ values: insValues }));

const selectLimit = vi.fn(() => Promise.resolve([]));
const selectWhere = vi.fn(() => ({ limit: selectLimit }));
const selectFrom = vi.fn(() => ({ where: selectWhere }));
const select = vi.fn(() => ({ from: selectFrom }));

vi.mock("@/lib/db", () => ({
  db: {
    update: (...a: unknown[]) => update(...a),
    insert: (...a: unknown[]) => insert(...a),
    select: (...a: unknown[]) => select(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const verifyPassphrase = vi.fn(async () => true);
vi.mock("@/lib/passphrase", () => ({
  verifyPassphrase: (...a: unknown[]) => verifyPassphrase(...a),
}));

import { updateLeaderStatus } from "@/app/discovery/actions";
import type { LeaderStatus } from "@/app/discovery/actions";

const CONFIRM_PHRASE = "確認執行";

describe("updateLeaderStatus", () => {
  beforeEach(() => {
    update.mockClear();
    updSet.mockClear();
    updWhere.mockClear();
    updReturning.mockClear().mockResolvedValue([{ wallet: "0xabc" }]);
    insert.mockClear();
    insValues.mockClear();
    insReturning.mockClear().mockResolvedValue([{ id: "job-1" }]);
  });

  it("accepts 'watching' (safe-direction, no gate) and returns { wallet, status }", async () => {
    const res = await updateLeaderStatus("0xabc", "watching");
    expect(res).toEqual({ wallet: "0xabc", status: "watching" });
    const set = updSet.mock.calls[0][0] as Record<string, unknown>;
    expect(set.user_status).toBe("watching");
  });

  it("accepts 'rejected' (safer-direction, no gate)", async () => {
    const res = await updateLeaderStatus("0xabc", "rejected");
    expect(res).toEqual({ wallet: "0xabc", status: "rejected" });
  });

  it("accepts 'following' when the confirm phrase matches", async () => {
    const res = await updateLeaderStatus("0xabc", "following", {
      confirmText: CONFIRM_PHRASE,
    });
    expect(res).toEqual({ wallet: "0xabc", status: "following" });
    const set = updSet.mock.calls[0][0] as Record<string, unknown>;
    expect(set.user_status).toBe("following");
  });

  it("rejects 'following' without the confirm phrase (no UPDATE)", async () => {
    await expect(
      updateLeaderStatus("0xabc", "following")
    ).rejects.toThrow(/確認執行/);
    expect(update).not.toHaveBeenCalled();
  });

  it("rejects 'following' when the confirm phrase is wrong (no UPDATE)", async () => {
    await expect(
      updateLeaderStatus("0xabc", "following", { confirmText: "wrong" })
    ).rejects.toThrow(/確認執行/);
    expect(update).not.toHaveBeenCalled();
  });

  it("rejects an invalid status before touching the DB (rev-web I-2)", async () => {
    await expect(
      updateLeaderStatus("0xabc", "deleted" as unknown as LeaderStatus)
    ).rejects.toThrow(/不允許的領單者狀態/);
    expect(update).not.toHaveBeenCalled();
  });

  it("rejects when no candidate row matched the wallet", async () => {
    updReturning.mockResolvedValueOnce([]);
    await expect(
      updateLeaderStatus("0xghost", "watching")
    ).rejects.toThrow(/找不到候選領單者/);
  });
});
