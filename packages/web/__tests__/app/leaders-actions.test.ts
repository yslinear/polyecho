import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * setFollowStatus server action — PRD §5.7 SAFE write.
 *
 * Contract:
 *  - status='paused'  -> UPDATE follow_relations SET status, paused_at=Date
 *  - status='active'  -> UPDATE follow_relations SET status, paused_at=null
 *  - returns { id, status }
 *
 * Drizzle (`@/lib/db`) is fully mocked: no DB, no network. The module
 * exposes NO live-trading enable/disable export (asserted below) because
 * that is CLI-only (PRD §5.7 high-danger tier).
 */

// .returning() resolves to the rows the UPDATE matched (empty when none).
const returning = vi.fn().mockResolvedValue([{ id: "rel-1" }]);
const updateWhere = vi.fn(() => ({ returning }));
const updateSet = vi.fn(() => ({ where: updateWhere }));
const update = vi.fn(() => ({ set: updateSet }));

vi.mock("@/lib/db", () => ({
  db: { update: (...a: unknown[]) => update(...a) },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import * as actions from "@/app/leaders/actions";
const { setFollowStatus } = actions;

describe("setFollowStatus", () => {
  beforeEach(() => {
    update.mockClear();
    updateSet.mockClear();
    updateWhere.mockClear();
    returning.mockClear().mockResolvedValue([{ id: "rel-1" }]);
  });

  it("pauses: sets status='paused' and a paused_at Date", async () => {
    const res = await setFollowStatus("rel-1", "paused");
    expect(res).toEqual({ id: "rel-1", status: "paused" });
    expect(update).toHaveBeenCalledTimes(1);
    const set = updateSet.mock.calls[0][0] as Record<string, unknown>;
    expect(set.status).toBe("paused");
    expect(set.paused_at).toBeInstanceOf(Date);
  });

  it("resumes: sets status='active' and clears paused_at to null", async () => {
    const res = await setFollowStatus("rel-2", "active");
    expect(res).toEqual({ id: "rel-2", status: "active" });
    const set = updateSet.mock.calls[0][0] as Record<string, unknown>;
    expect(set.status).toBe("active");
    expect(set.paused_at).toBeNull();
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
