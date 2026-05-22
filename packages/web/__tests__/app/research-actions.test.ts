import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * promoteToLiveFollow Server Action — C1-P5 money-safety gate.
 *
 * The SINGLE explicit path from the C1 paper ladder to a real follow. The
 * test asserts the non-negotiable money-safety contract (roadmap C1):
 *   - danger-passphrase gate: a wrong phrase / passphrase rejects and writes
 *     NOTHING (no follow_relations INSERT);
 *   - server-side state rejection: a candidate that is NOT `trial_ready`, or
 *     is `locked`, is rejected even when the gate passes;
 *   - the inserted follow_relations row is status='paused' (NOT active) —
 *     the second lock; the operator must still Resume on /leaders;
 *   - idempotency: a leader already followed is not re-inserted.
 *
 * Drizzle (`@/lib/db`) + the inline-confirm gate are fully mocked: no DB.
 * The gate mock lets each test flip the passphrase result without touching
 * the constant-time crypto, and routes the (best-effort, never-throwing)
 * audit emit through a no-op so it does not add a spurious `insert` call.
 */

// --- Drizzle mock ----------------------------------------------------------
// select() is used for: candidate lookup, existing-follow lookup, account
// default, template default. Each call returns a thenable chain whose final
// .limit() resolves to a queued row-set. A small queue lets each test script
// the sequence of SELECT results.
let selectQueue: unknown[][] = [];
const selectLimit = vi.fn(() => Promise.resolve(selectQueue.shift() ?? []));
const selectOrderBy = vi.fn(() => ({ limit: selectLimit }));
const selectWhere = vi.fn(() => ({ limit: selectLimit, orderBy: selectOrderBy }));
const selectFrom = vi.fn(() => ({
  where: selectWhere,
  orderBy: selectOrderBy,
  limit: selectLimit,
}));
const select = vi.fn(() => ({ from: selectFrom }));

const insertReturning = vi.fn(() => Promise.resolve([{ id: "fr-new" }]));
const insertValues = vi.fn(() => ({ returning: insertReturning }));
const insert = vi.fn(() => ({ values: insertValues }));

vi.mock("@/lib/db", () => ({
  db: {
    select: (...a: unknown[]) => select(...a),
    insert: (...a: unknown[]) => insert(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

// The shared inline-confirm gate. `checkInlineConfirm` is the danger-
// passphrase lock — the mock returns `gateResult` so each test can prove a
// rejected gate writes NOTHING without re-implementing the crypto.
// `emitInlineConfirmAudit` is a best-effort no-op here so the audit `events`
// INSERT does not inflate the `insert` call count we assert on.
let gateResult: { ok: boolean; message: string } = { ok: true, message: "" };
const checkInlineConfirm = vi.fn(async () => gateResult);
const emitInlineConfirmAudit = vi.fn(async () => undefined);
vi.mock("@/lib/inline-confirm-gate", () => ({
  checkInlineConfirm: (...a: unknown[]) => checkInlineConfirm(...a),
  emitInlineConfirmAudit: (...a: unknown[]) => emitInlineConfirmAudit(...a),
}));

import { promoteToLiveFollow, isPromotable } from "@/app/research/actions";

const CONFIRM_PHRASE = "確認執行";
const LEADER = "0x1111111111111111111111111111111111111111";

/**
 * Queue the SELECT result-sets for one successful-shaped run:
 *   1. candidate lookup, 2. existing-follow lookup,
 *   3. account default,  4. template default.
 */
function queueHappyPath(
  candidate: Record<string, unknown> | null,
  existingFollow = false,
  hasAccount = true,
  hasTemplate = true
) {
  selectQueue = [
    candidate ? [candidate] : [],
    existingFollow ? [{ id: "fr-existing" }] : [],
    hasAccount ? [{ id: "acct-1" }] : [],
    hasTemplate ? [{ id: "tpl-1" }] : [],
  ];
}

const trialReady = {
  wallet_address: LEADER,
  lifecycle_state: "trial_ready",
  locked: false,
};

beforeEach(() => {
  select.mockClear();
  insert.mockClear();
  insertValues.mockClear();
  insertReturning.mockClear().mockResolvedValue([{ id: "fr-new" }]);
  checkInlineConfirm.mockClear();
  emitInlineConfirmAudit.mockClear();
  gateResult = { ok: true, message: "" };
  selectQueue = [];
});

describe("promoteToLiveFollow — danger-passphrase gate", () => {
  it("requires the danger passphrase: calls the gate with requirePassphrase=true", async () => {
    queueHappyPath(trialReady);
    await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    const arg = checkInlineConfirm.mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(arg.requirePassphrase).toBe(true);
    expect(arg.confirmText).toBe(CONFIRM_PHRASE);
    expect(arg.passphrase).toBe("pw");
  });

  it("rejects when the danger gate fails (wrong phrase / passphrase) — writes NOTHING", async () => {
    gateResult = { ok: false, message: "通行碼錯誤，未執行任何操作。請重試。" };
    queueHappyPath(trialReady);
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: "nope",
      passphrase: "wrong",
    });
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/通行碼/);
    expect(insert).not.toHaveBeenCalled();
    // the gate is the FIRST check — no candidate SELECT happens either.
    expect(select).not.toHaveBeenCalled();
  });
});

describe("promoteToLiveFollow — server-side state rejection", () => {
  it("rejects a non-trial_ready candidate even when the gate passes", async () => {
    queueHappyPath({
      wallet_address: LEADER,
      lifecycle_state: "paper",
      locked: false,
    });
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/trial_ready/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects a locked candidate even when trial_ready", async () => {
    queueHappyPath({
      wallet_address: LEADER,
      lifecycle_state: "trial_ready",
      locked: true,
    });
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/鎖定/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects an unknown candidate", async () => {
    queueHappyPath(null);
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects a leader already followed (idempotent — no dup INSERT)", async () => {
    queueHappyPath(trialReady, /* existingFollow */ true);
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });
});

describe("promoteToLiveFollow — the PAUSED insert", () => {
  it("inserts ONE follow_relations row at status='paused' (NOT active)", async () => {
    queueHappyPath(trialReady);
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(true);
    expect(insert).toHaveBeenCalledTimes(1);
    const values = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(values.status).toBe("paused");
    expect(values.status).not.toBe("active");
    expect(values.paused_at).toBeInstanceOf(Date);
    expect(values.leader_address).toBe(LEADER);
    // links a default account + template (the NOT-NULL FKs).
    expect(values.account_id).toBe("acct-1");
    expect(values.template_id).toBe("tpl-1");
  });

  it("rejects when no account exists (cannot link the NOT-NULL FK)", async () => {
    queueHappyPath(trialReady, false, /* hasAccount */ false);
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects when no copy template exists", async () => {
    queueHappyPath(trialReady, false, true, /* hasTemplate */ false);
    const res = await promoteToLiveFollow({
      leaderAddress: LEADER,
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });
});

describe("isPromotable guard", () => {
  it("is true ONLY for an unlocked trial_ready candidate", async () => {
    expect(await isPromotable("trial_ready", false)).toBe(true);
    expect(await isPromotable("trial_ready", true)).toBe(false);
    expect(await isPromotable("paper", false)).toBe(false);
    expect(await isPromotable("cooldown", false)).toBe(false);
    expect(await isPromotable("discovered", false)).toBe(false);
  });
});
