import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * /alerts Server Actions (PRD §5.7 + §5.8).
 *
 * Contract under test:
 *  - getVapidPublicKey reads the vapid_public_key row and NEVER touches a
 *    private key (only `settings` is queried; nothing returns the secret).
 *  - subscribePush UPSERTs on the UNIQUE endpoint (insert ->
 *    onConflictDoUpdate) and rejects incomplete subscriptions.
 *  - createPendingAction inserts awaiting_confirm with a ~300s TTL.
 *  - confirmPendingAction: correct passphrase confirms, wrong rejects
 *    WITHOUT consuming the row, expired is rejected, and the stored hash is
 *    the self-describing pbkdf2$iter$salt$hash format + timing-safe.
 *
 * Drizzle is fully mocked; crypto is real (fast at low iterations via env).
 */

process.env.POLYECHO_KEY_KDF_ITERATIONS = "1000"; // keep tests fast

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
let selectResult: unknown[] = [];
const limit = vi.fn(() => Promise.resolve(selectResult));
const orderBy = vi.fn(() => Promise.resolve(selectResult));
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
  createPendingAction,
  confirmPendingAction,
} from "@/app/alerts/actions";

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

describe("createPendingAction", () => {
  it("inserts awaiting_confirm with a ~300s expiry", async () => {
    const before = Date.now();
    const res = await createPendingAction("manual_sell", { target: "m1" });
    expect(res.ok).toBe(true);
    expect(res.id).toBe("pa-1");
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.action_type).toBe("manual_sell");
    expect(row.status).toBe("awaiting_confirm");
    const exp = (row.expires_at as Date).getTime();
    expect(exp).toBeGreaterThanOrEqual(before + 299_000);
    expect(exp).toBeLessThanOrEqual(Date.now() + 301_000);
  });

  it("rejects an unknown action type", async () => {
    const res = await createPendingAction(
      "nope" as unknown as "manual_sell",
      {}
    );
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });

  it("ALSO emits the frozen pending_dangerous_action events row + NOTIFY", async () => {
    const res = await createPendingAction("emergency_liquidate", {
      target: "m9",
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
    returning.mockImplementationOnce(() =>
      Promise.resolve([{ id: "pa-1" }])
    ); // pending insert OK
    returning.mockImplementationOnce(() =>
      Promise.reject(new Error("events insert boom"))
    ); // events insert throws
    const res = await createPendingAction("manual_sell", { target: "m1" });
    // Pending action creation MUST still succeed (mirrors Python best-effort).
    expect(res.ok).toBe(true);
    expect(res.id).toBe("pa-1");
    // NOTIFY never reached because the events insert threw.
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("confirmPendingAction", () => {
  const future = () => new Date(Date.now() + 60_000);

  it("confirms with the correct passphrase and stores a self-describing hash", async () => {
    // First select: the awaiting row with NO hash yet.
    selectResult = [
      {
        id: "pa-1",
        status: "awaiting_confirm",
        expires_at: future(),
        confirm_passphrase_hash: null,
      },
    ];
    const res = await confirmPendingAction("pa-1", "correct horse");
    expect(res.ok).toBe(true);
    // update called twice: once to set the hash, once to set status=confirmed.
    expect(update).toHaveBeenCalledTimes(2);
    const hashSet = updateSet.mock.calls[0][0] as Record<string, string>;
    expect(hashSet.confirm_passphrase_hash).toMatch(
      /^pbkdf2\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$/
    );
    const statusSet = updateSet.mock.calls[1][0] as Record<string, string>;
    expect(statusSet.status).toBe("confirmed");
  });

  it("rejects the wrong passphrase WITHOUT consuming the row", async () => {
    // Pre-existing hash for passphrase "right".
    const { hashPassphrase } = await import("@/lib/passphrase");
    const stored = await hashPassphrase("right");
    selectResult = [
      {
        id: "pa-1",
        status: "awaiting_confirm",
        expires_at: future(),
        confirm_passphrase_hash: stored,
      },
    ];
    const res = await confirmPendingAction("pa-1", "wrong");
    expect(res.ok).toBe(false);
    // No status flip — the row is NOT consumed (TTL handles expiry).
    const flips = updateSet.mock.calls.filter(
      (c) => (c[0] as Record<string, unknown>).status === "confirmed"
    );
    expect(flips).toHaveLength(0);
  });

  it("rejects an expired row", async () => {
    selectResult = [
      {
        id: "pa-1",
        status: "awaiting_confirm",
        expires_at: new Date(Date.now() - 1000),
        confirm_passphrase_hash: null,
      },
    ];
    const res = await confirmPendingAction("pa-1", "x");
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/逾時/);
  });

  it("rejects a row that is not awaiting_confirm", async () => {
    selectResult = [
      {
        id: "pa-1",
        status: "confirmed",
        expires_at: future(),
        confirm_passphrase_hash: null,
      },
    ];
    const res = await confirmPendingAction("pa-1", "x");
    expect(res.ok).toBe(false);
  });

  it("rejects when the row does not exist", async () => {
    selectResult = [];
    const res = await confirmPendingAction("missing", "x");
    expect(res.ok).toBe(false);
  });
});
