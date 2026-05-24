import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * addCandidateLeader Server Action — manual operator add of a smart-wallet.
 *
 * This action is NOT money-path: it only inserts an OBSERVATION row at
 * lifecycle_state='discovered' so the discovery + paper workers pick the
 * wallet up on their next cycle. The tests pin the three contracts:
 *
 *   1. Input-guard tier: an invalid address shape is rejected BEFORE any
 *      DB call — no SELECT, no INSERT.
 *   2. Duplicate handling: a SELECT that returns a row short-circuits the
 *      INSERT and reports the EXISTING `lifecycle_state` so the client can
 *      render the friendly "已在追蹤" hint.
 *   3. Happy path: the INSERT carries sources=['manual'], a non-null
 *      last_active_at / last_seen_ts / first_discovered_at, and
 *      lifecycle_state='discovered'. The address is lowercased before
 *      either the SELECT or the INSERT.
 *
 * Drizzle (`@/lib/db`) and `next/cache` are fully mocked: no DB / no
 * router. `revalidatePath` is asserted on the happy path so the /discovery
 * server component refresh is part of the contract.
 */

// --- Drizzle mock ----------------------------------------------------------
// select() is used by the duplicate check. The queue lets each test script
// the SELECT result (empty array = no duplicate; [row] = duplicate).
let selectQueue: unknown[][] = [];
const selectLimit = vi.fn(() => Promise.resolve(selectQueue.shift() ?? []));
const selectWhere = vi.fn(() => ({ limit: selectLimit }));
const selectFrom = vi.fn(() => ({ where: selectWhere, limit: selectLimit }));
const select = vi.fn(() => ({ from: selectFrom }));

const insertReturning = vi.fn(() => Promise.resolve([{ id: "bt-1" }]));
const insertValues = vi.fn(() => ({
  returning: insertReturning,
  // The addCandidateLeader path awaits insert().values(...) directly (no
  // returning()), so the chain must itself be thenable.
  then: (cb: (v: undefined) => unknown) => Promise.resolve(undefined).then(cb),
}));
const insert = vi.fn(() => ({ values: insertValues }));

vi.mock("@/lib/db", () => ({
  db: {
    select: (...a: unknown[]) => select(...a),
    insert: (...a: unknown[]) => insert(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const revalidatePath = vi.fn();
vi.mock("next/cache", () => ({
  revalidatePath: (...a: unknown[]) => revalidatePath(...a),
}));

import { addCandidateLeader } from "@/app/discovery/actions";

const ADDR_LOWER = "0xabcdef0123456789abcdef0123456789abcdef01";
const ADDR_MIXED = "0xABCDEF0123456789abcdef0123456789ABCDEF01";

beforeEach(() => {
  select.mockClear();
  selectFrom.mockClear();
  selectWhere.mockClear();
  selectLimit.mockClear();
  insert.mockClear();
  insertValues.mockClear();
  insertReturning.mockClear();
  revalidatePath.mockClear();
  selectQueue = [];
});

describe("addCandidateLeader — input guard", () => {
  it("rejects an empty address (no SELECT, no INSERT)", async () => {
    const res = await addCandidateLeader({ address: "" });
    expect(res).toEqual({ ok: false, reason: "invalid_address" });
    expect(select).not.toHaveBeenCalled();
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects a non-EVM-shaped address (no SELECT, no INSERT)", async () => {
    const res = await addCandidateLeader({ address: "not-an-address" });
    expect(res).toEqual({ ok: false, reason: "invalid_address" });
    expect(select).not.toHaveBeenCalled();
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects an address with wrong hex length", async () => {
    const res = await addCandidateLeader({ address: "0x123" });
    expect(res).toEqual({ ok: false, reason: "invalid_address" });
    expect(insert).not.toHaveBeenCalled();
  });
});

describe("addCandidateLeader — duplicate handling", () => {
  it("returns already_tracked + the existing lifecycle_state when a row exists", async () => {
    selectQueue = [[{ lifecycle_state: "paper" }]];
    const res = await addCandidateLeader({ address: ADDR_LOWER });
    expect(res).toEqual({
      ok: false,
      reason: "already_tracked",
      lifecycle_state: "paper",
    });
    // SELECT happened, INSERT did not.
    expect(select).toHaveBeenCalledTimes(1);
    expect(insert).not.toHaveBeenCalled();
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("defaults the duplicate lifecycle_state to 'discovered' when the row's column is null", async () => {
    selectQueue = [[{ lifecycle_state: null }]];
    const res = await addCandidateLeader({ address: ADDR_LOWER });
    expect(res).toEqual({
      ok: false,
      reason: "already_tracked",
      lifecycle_state: "discovered",
    });
    expect(insert).not.toHaveBeenCalled();
  });
});

describe("addCandidateLeader — happy path", () => {
  it("inserts a new row with sources=['manual'], lifecycle_state='discovered', timestamps set, and revalidates /discovery", async () => {
    selectQueue = [[]];
    const res = await addCandidateLeader({
      address: ADDR_LOWER,
      note: "operator-added from Twitter mention",
    });
    expect(res).toEqual({ ok: true });
    expect(insert).toHaveBeenCalledTimes(1);
    const values = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(values.wallet_address).toBe(ADDR_LOWER);
    expect(values.sources).toEqual(["manual"]);
    expect(values.lifecycle_state).toBe("discovered");
    expect(values.user_notes).toBe("operator-added from Twitter mention");
    expect(values.last_active_at).toBeInstanceOf(Date);
    expect(values.last_seen_ts).toBeInstanceOf(Date);
    expect(values.first_discovered_at).toBeInstanceOf(Date);
    expect(values.updated_at).toBeInstanceOf(Date);
    expect(revalidatePath).toHaveBeenCalledWith("/discovery");
  });

  it("lowercases a mixed-case address before SELECT and INSERT", async () => {
    selectQueue = [[]];
    await addCandidateLeader({ address: ADDR_MIXED });
    expect(insert).toHaveBeenCalledTimes(1);
    const values = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(values.wallet_address).toBe(ADDR_LOWER);
    expect(values.wallet_address).not.toBe(ADDR_MIXED);
  });

  it("stores an omitted / blank note as NULL", async () => {
    selectQueue = [[]];
    await addCandidateLeader({ address: ADDR_LOWER, note: "   " });
    const values = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(values.user_notes).toBeNull();
  });
});
