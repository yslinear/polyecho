import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * /alerts Server Actions (PRD §5.7 + §5.8).
 *
 * Contract under test:
 *  - getVapidPublicKey reads the vapid_public_key row and NEVER touches a
 *    private key (only `settings` is queried; nothing returns the secret).
 *  - subscribePush UPSERTs on the UNIQUE endpoint (insert ->
 *    onConflictDoUpdate) and rejects incomplete subscriptions.
 *  - confirmDangerousAction (INLINE single-step): rejects an unknown type;
 *    rejects a non-verbatim type-to-confirm phrase WITHOUT touching the
 *    DB; fail-closed when settings.danger_passphrase_hash is unset; with a
 *    correct Python-format pbkdf2$... hash + the verbatim phrase + the
 *    right passphrase it INSERTS one row DIRECTLY as status `confirmed`
 *    (NO confirm_passphrase_hash, params carry an _inline_confirmed_at
 *    marker) and ALSO emits the frozen pending_dangerous_action event;
 *    a WRONG passphrase / sub-floor hash writes NOTHING.
 *
 * Drizzle is fully mocked; crypto is real (clamped to the 100000 floor).
 */

process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000"; // clamped floor; fast enough

// Real schema table objects: used for insert-target identity in the mock
// and to assert the events row goes to the `events` table.
import { events as eventsTable } from "@/lib/db/schema";

// --- Drizzle mock: a chainable builder recording calls --------------------
const onConflictDoUpdate = vi.fn().mockResolvedValue(undefined);
// pending_actions insert -> [{ id: "pa-1" }]; events insert -> [{ id: 42 }].
// Keyed off the table object passed to db.insert(table).
let lastInsertTable: unknown;
const returning = vi.fn(() =>
  Promise.resolve(
    lastInsertTable === eventsTable ? [{ id: 42 }] : [{ id: "pa-1" }]
  )
);
const insertValues = vi.fn(() => ({ onConflictDoUpdate, returning }));
const insert = vi.fn((table?: unknown) => {
  lastInsertTable = table;
  return { values: insertValues };
});

// db.execute(sql`...`) — the pg_notify path. Records the tagged-template
// SQL fragment so tests can assert the channel + bound id.
const execute = vi.fn().mockResolvedValue([]);

const deleteWhere = vi.fn().mockResolvedValue(undefined);
const del = vi.fn(() => ({ where: deleteWhere }));

const updateWhere = vi.fn().mockResolvedValue(undefined);
const updateSet = vi.fn(() => ({ where: updateWhere }));
const update = vi.fn(() => ({ set: updateSet }));

// select(...).from(...).where(...).limit(...) | .orderBy(...)
//
// `selectResult` is the default single result. `selectQueue`, when
// non-empty, is consumed one entry per `.limit()` call (kept for any
// multi-select callers). confirmDangerousAction does exactly ONE select
// (the settings.danger_passphrase_hash row) before its conditional insert.
let selectResult: unknown[] = [];
let selectQueue: unknown[][] | null = null;
const nextSelect = () => {
  if (selectQueue && selectQueue.length > 0) {
    return Promise.resolve(selectQueue.shift() as unknown[]);
  }
  return Promise.resolve(selectResult);
};
const limit = vi.fn(() => nextSelect());
const orderBy = vi.fn(() => nextSelect());
const where = vi.fn(() => ({ limit, orderBy }));
const from = vi.fn(() => ({ where }));
const select = vi.fn(() => ({ from }));

vi.mock("@/lib/db", () => ({
  db: {
    insert: (...a: unknown[]) => insert(...a),
    delete: (...a: unknown[]) => del(...a),
    update: (...a: unknown[]) => update(...a),
    select: (...a: unknown[]) => select(...a),
    execute: (...a: unknown[]) => execute(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import {
  getVapidPublicKey,
  subscribePush,
  unsubscribePush,
  confirmDangerousAction,
} from "@/app/alerts/actions";
import { CONFIRM_PHRASE } from "@/app/alerts/dangerous-shared";

beforeEach(() => {
  insert.mockClear();
  insertValues.mockClear();
  onConflictDoUpdate.mockClear().mockResolvedValue(undefined);
  returning.mockClear();
  execute.mockClear().mockResolvedValue([]);
  lastInsertTable = undefined;
  del.mockClear();
  deleteWhere.mockClear().mockResolvedValue(undefined);
  update.mockClear();
  updateSet.mockClear();
  updateWhere.mockClear().mockResolvedValue(undefined);
  select.mockClear();
  selectResult = [];
  selectQueue = null;
});

describe("getVapidPublicKey", () => {
  it("returns the public key string and only queries `settings`", async () => {
    selectResult = [{ value: "BPUBLICKEYbase64url" }];
    const key = await getVapidPublicKey();
    expect(key).toBe("BPUBLICKEYbase64url");
    // It must NOT insert/update/delete anything (read-only).
    expect(insert).not.toHaveBeenCalled();
    expect(update).not.toHaveBeenCalled();
    expect(del).not.toHaveBeenCalled();
  });

  it("never returns a private key: tolerates {value} shape, defaults empty", async () => {
    selectResult = [{ value: { value: "Bpub" } }];
    expect(await getVapidPublicKey()).toBe("Bpub");
    selectResult = [];
    expect(await getVapidPublicKey()).toBe("");
  });
});

describe("subscribePush", () => {
  it("UPSERTs on the UNIQUE endpoint with keys + device label", async () => {
    const res = await subscribePush(
      { endpoint: "https://push.example/abc", keys: { p256dh: "P", auth: "A" } },
      "iOS Safari"
    );
    expect(res.ok).toBe(true);
    expect(insert).toHaveBeenCalledTimes(1);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.endpoint).toBe("https://push.example/abc");
    expect(row.p256dh_key).toBe("P");
    expect(row.auth_key).toBe("A");
    expect(row.device_label).toBe("iOS Safari");
    const conflict = onConflictDoUpdate.mock.calls[0][0] as {
      set: Record<string, unknown>;
    };
    expect(conflict.set.p256dh_key).toBe("P");
    expect(conflict.set.device_label).toBe("iOS Safari");
  });

  it("rejects an incomplete subscription without writing", async () => {
    const res = await subscribePush(
      { endpoint: "", keys: { p256dh: "", auth: "" } },
      "x"
    );
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });
});

describe("unsubscribePush", () => {
  it("deletes the row for the endpoint", async () => {
    const res = await unsubscribePush("https://push.example/abc");
    expect(res.ok).toBe(true);
    expect(del).toHaveBeenCalledTimes(1);
  });
});

describe("confirmDangerousAction (INLINE single-step)", () => {
  it("rejects an unknown action type without touching the DB", async () => {
    const res = await confirmDangerousAction({
      actionType: "nope" as unknown as "manual_sell",
      params: {},
      confirmText: CONFIRM_PHRASE,
      passphrase: "x",
    });
    expect(res.ok).toBe(false);
    expect(select).not.toHaveBeenCalled();
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects a non-verbatim type-to-confirm phrase without touching the DB", async () => {
    const res = await confirmDangerousAction({
      actionType: "manual_sell",
      params: { target: "m1" },
      confirmText: "確認", // not the full verbatim phrase
      passphrase: "secret",
    });
    expect(res.ok).toBe(false);
    // The phrase gate is BEFORE any DB read: no select, no insert.
    expect(select).not.toHaveBeenCalled();
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects an empty passphrase even with the verbatim phrase", async () => {
    const res = await confirmDangerousAction({
      actionType: "manual_sell",
      params: { target: "m1" },
      confirmText: CONFIRM_PHRASE,
      passphrase: "",
    });
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });

  it("FAILS CLOSED when settings.danger_passphrase_hash is unset", async () => {
    selectResult = []; // settings.danger_passphrase_hash lookup -> none
    const res = await confirmDangerousAction({
      actionType: "manual_sell",
      params: { target: "m1" },
      confirmText: CONFIRM_PHRASE,
      passphrase: "anything",
    });
    expect(res.ok).toBe(false);
    expect(res.message).toContain("set-danger-passphrase");
    // Never auto-provision, NEVER write a row.
    expect(insert).not.toHaveBeenCalled();
  });

  it("verifies a Python-format pbkdf2 hash and inserts a CONFIRMED row", async () => {
    const { hashPassphrase } = await import("@/lib/passphrase");
    const stored = await hashPassphrase("correct horse"); // canonical format
    selectResult = [{ value: stored }]; // operator-provisioned hash
    const before = Date.now();
    const res = await confirmDangerousAction({
      actionType: "manual_sell",
      params: { target: "m1", size: "100" },
      confirmText: CONFIRM_PHRASE,
      passphrase: "correct horse",
    });
    expect(res.ok).toBe(true);
    expect(res.id).toBe("pa-1");

    // First insert is the pending_actions row, written DIRECTLY as
    // status `confirmed` (no transient awaiting_confirm row).
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.action_type).toBe("manual_sell");
    expect(row.status).toBe("confirmed");
    // NO passphrase material is ever written.
    expect("confirm_passphrase_hash" in row).toBe(false);
    // params carry the original params PLUS an _inline_confirmed_at audit
    // marker (an ISO timestamp ~now); core/pending ignores unknown keys.
    const rowParams = row.params as Record<string, unknown>;
    expect(rowParams.target).toBe("m1");
    expect(rowParams.size).toBe("100");
    const stampMs = new Date(
      rowParams._inline_confirmed_at as string
    ).getTime();
    expect(stampMs).toBeGreaterThanOrEqual(before - 1000);
    expect(stampMs).toBeLessThanOrEqual(Date.now() + 1000);
    // No UPDATE path at all in the inline flow (it inserts directly).
    expect(update).not.toHaveBeenCalled();
  });

  it("ALSO emits the frozen pending_dangerous_action events row + NOTIFY", async () => {
    const { hashPassphrase } = await import("@/lib/passphrase");
    selectResult = [{ value: await hashPassphrase("pw") }];
    const res = await confirmDangerousAction({
      actionType: "emergency_liquidate",
      params: { target: "m9" },
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(true);

    // Two inserts: pending_actions (call 0) THEN events (call 1).
    expect(insert).toHaveBeenCalledTimes(2);
    expect(insert.mock.calls[1][0]).toBe(eventsTable);

    const ev = insertValues.mock.calls[1][0] as Record<string, unknown>;
    expect(ev.event_type).toBe("pending_dangerous_action");
    expect(ev.severity).toBe("warning");
    // pushed_at left NULL == omitted from the insert values (sender stamps it).
    expect("pushed_at" in ev).toBe(false);

    const payload = ev.payload as {
      data: Record<string, unknown>;
      push: Record<string, unknown>;
    };
    // Frozen jsonb shape: { data: <payload>, push: <PushPayload.to_json()> }.
    // params_summary is the ORIGINAL params (no audit marker leaked here).
    expect(payload.data).toEqual({
      pending_action_id: "pa-1",
      action_type: "emergency_liquidate",
      params_summary: { target: "m9" },
    });
    expect(payload.push.title).toBe("待確認的危險操作");
    expect(payload.push.url).toBe("/alerts");
    expect(payload.push.tag).toBe("pending:pa-1");
    expect(payload.push.data).toEqual({ url: "/alerts" });
    expect(typeof payload.push.body).toBe("string");
    // PushPayload.to_json() frozen key set: {title,body,url,tag,data}.
    expect(Object.keys(payload.push).sort()).toEqual(
      ["body", "data", "tag", "title", "url"].sort()
    );

    // NOTIFY issued exactly once on channel `polyecho_events` with the
    // events.id rendered as a string, AFTER the events row insert.
    expect(execute).toHaveBeenCalledTimes(1);
    const notifySql = JSON.stringify(execute.mock.calls[0][0]);
    expect(notifySql).toContain("pg_notify");
    expect(notifySql).toContain("polyecho_events");
    expect(notifySql).toContain("42");
  });

  it("is best-effort: events insert failure still returns success", async () => {
    const { hashPassphrase } = await import("@/lib/passphrase");
    selectResult = [{ value: await hashPassphrase("pw") }];
    returning.mockImplementationOnce(() =>
      Promise.resolve([{ id: "pa-1" }])
    ); // pending insert OK (confirmed)
    returning.mockImplementationOnce(() =>
      Promise.reject(new Error("events insert boom"))
    ); // events insert throws
    const res = await confirmDangerousAction({
      actionType: "manual_sell",
      params: { target: "m1" },
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    // The confirmed action MUST still succeed (mirrors Python best-effort).
    expect(res.ok).toBe(true);
    expect(res.id).toBe("pa-1");
    // NOTIFY never reached because the events insert threw.
    expect(execute).not.toHaveBeenCalled();
  });

  it("rejects the WRONG passphrase and writes NOTHING", async () => {
    const { hashPassphrase } = await import("@/lib/passphrase");
    selectResult = [{ value: await hashPassphrase("right") }];
    const res = await confirmDangerousAction({
      actionType: "manual_sell",
      params: { target: "m1" },
      confirmText: CONFIRM_PHRASE,
      passphrase: "wrong",
    });
    expect(res.ok).toBe(false);
    // No row is created on a bad passphrase — the gate is BEFORE the write.
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects a stored hash whose iterations are below the 100000 floor", async () => {
    const { pbkdf2Sync, randomBytes } = await import("node:crypto");
    const salt = randomBytes(16);
    const dk = pbkdf2Sync(
      Buffer.from("pw", "utf8"),
      salt,
      99_999,
      32,
      "sha256"
    );
    const tampered = `pbkdf2$99999$${salt.toString(
      "base64"
    )}$${dk.toString("base64")}`;
    selectResult = [{ value: tampered }];
    // Even the correct passphrase must NOT confirm a sub-floor hash.
    const res = await confirmDangerousAction({
      actionType: "manual_sell",
      params: { target: "m1" },
      confirmText: CONFIRM_PHRASE,
      passphrase: "pw",
    });
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });
});
