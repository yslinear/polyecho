import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * setFollowStatus server action — PRD §5.7 SAFE write.
 *
 * Contract:
 *  - status='paused'  -> UPDATE follow_relations SET status, paused_at=Date
 *  - status='active'  -> UPDATE follow_relations SET status, paused_at=null
 *  - returns { id, status }
 *
 * P7d / R24: status='active' (RESUME) is risk-increasing and must pass the
 * inline-confirm gate (CONFIRM_PHRASE only) BEFORE the UPDATE; 'paused'
 * stays safe-direction (no gate). The module exposes NO live-trading
 * enable/disable export (CLI-only, PRD §5.7).
 *
 * Drizzle (`@/lib/db`) is fully mocked: no DB, no network.
 */

// .returning() resolves to the rows the UPDATE matched (empty when none).
const returning = vi.fn().mockResolvedValue([{ id: "rel-1" }]);
const updateWhere = vi.fn(() => ({ returning }));
const updateSet = vi.fn(() => ({ where: updateWhere }));
const update = vi.fn(() => ({ set: updateSet }));

const insertReturning = vi.fn().mockResolvedValue([{ id: 1 }]);
const insertValues = vi.fn(() => ({
  returning: insertReturning,
  onConflictDoUpdate: vi.fn().mockResolvedValue(undefined),
}));
const insert = vi.fn(() => ({ values: insertValues }));

// danger_passphrase_hash row read by the gate (not used by setFollowStatus,
// which is CONFIRM_PHRASE-only; declared for parity with the gate shape).
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

import * as actions from "@/app/leaders/actions";
const { setFollowStatus } = actions;

const CONFIRM_PHRASE = "確認執行";

describe("setFollowStatus", () => {
  beforeEach(() => {
    update.mockClear();
    updateSet.mockClear();
    updateWhere.mockClear();
    returning.mockClear().mockResolvedValue([{ id: "rel-1" }]);
    insert.mockClear();
    insertValues.mockClear();
    insertReturning.mockClear().mockResolvedValue([{ id: 1 }]);
  });

  it("pauses: sets status='paused' and a paused_at Date (safe-direction, no gate)", async () => {
    const res = await setFollowStatus("rel-1", "paused");
    expect(res).toEqual({ id: "rel-1", status: "paused" });
    expect(update).toHaveBeenCalledTimes(1);
    const set = updateSet.mock.calls[0][0] as Record<string, unknown>;
    expect(set.status).toBe("paused");
    expect(set.paused_at).toBeInstanceOf(Date);
  });

  it("resumes: sets status='active' and clears paused_at to null when phrase matches", async () => {
    const res = await setFollowStatus("rel-2", "active", {
      confirmText: CONFIRM_PHRASE,
    });
    expect(res).toEqual({ id: "rel-2", status: "active" });
    const set = updateSet.mock.calls[0][0] as Record<string, unknown>;
    expect(set.status).toBe("active");
    expect(set.paused_at).toBeNull();
  });

  it("rejects resume without the confirm phrase (no UPDATE)", async () => {
    await expect(setFollowStatus("rel-2", "active")).rejects.toThrow(
      /確認執行/
    );
    expect(update).not.toHaveBeenCalled();
  });

  it("rejects resume when the confirm phrase is wrong (no UPDATE)", async () => {
    await expect(
      setFollowStatus("rel-2", "active", { confirmText: "nope" })
    ).rejects.toThrow(/確認執行/);
    expect(update).not.toHaveBeenCalled();
  });

  it("rejects (throws) when no row matched the id (rev-web I-1)", async () => {
    returning.mockResolvedValueOnce([]);
    await expect(setFollowStatus("ghost-id", "paused")).rejects.toThrow(
      /找不到跟單關係/
    );
  });

  it("exposes NO live-trading enable/disable action (CLI-only, PRD §5.7)", () => {
    const exported = Object.keys(actions);
    expect(exported).toContain("setFollowStatus");
    for (const name of exported) {
      expect(name.toLowerCase()).not.toMatch(
        /livetrading|enablelive|disablelive|setlive/
      );
    }
  });
});
