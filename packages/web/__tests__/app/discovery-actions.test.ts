import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * Discovery Server Actions — rev-web I-2 server-side validation.
 *
 *  - updateLeaderStatus: allowlist of status; .returning() existence check.
 *  - valid status still updates and returns { wallet, status }.
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

vi.mock("@/lib/db", () => ({
  db: {
    update: (...a: unknown[]) => update(...a),
    insert: (...a: unknown[]) => insert(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import { updateLeaderStatus } from "@/app/discovery/actions";
import type { LeaderStatus } from "@/app/discovery/actions";

describe("updateLeaderStatus", () => {
  beforeEach(() => {
    update.mockClear();
    updSet.mockClear();
    updWhere.mockClear();
    updReturning.mockClear().mockResolvedValue([{ wallet: "0xabc" }]);
  });

  it("accepts a valid status and returns { wallet, status }", async () => {
    const res = await updateLeaderStatus("0xabc", "following");
    expect(res).toEqual({ wallet: "0xabc", status: "following" });
    const set = updSet.mock.calls[0][0] as Record<string, unknown>;
    expect(set.user_status).toBe("following");
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
