import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * Account import Server Action — the operator-authorised (recorded) web
 * private-key-import override. The crypto half is exhaustively covered by
 * __tests__/lib/key-crypto.test.ts + the cross-language pytest; THIS file
 * closes the previously-untested gap on the Server Action's own logic:
 * input validation, funder-resolution rules, the fail-closed danger gate,
 * and the atomic upsert + created/updated labelling.
 *
 * Drizzle (`@/lib/db`) is fully mocked (no DB, no network). `verifyPassphrase`
 * / `hashPassphrase` / `encryptPrivateKey` / the shape regexes run FOR REAL
 * (low KDF iters) so the constant-time gate and byte-blob encryption are
 * exercised end to end, not stubbed away.
 */

// --- danger-hash SELECT chain: db.select(..).from(..).where(..).limit(1) --
const selLimit = vi.fn();
const selWhere = vi.fn(() => ({ limit: selLimit }));
const selFrom = vi.fn(() => ({ where: selWhere }));
const select = vi.fn(() => ({ from: selFrom }));

// --- atomic upsert chain: db.insert(..).values(..).onConflictDoUpdate(..).returning(..) --
const insReturning = vi.fn();
const insOnConflict = vi.fn(() => ({ returning: insReturning }));
const insValues = vi.fn(() => ({ onConflictDoUpdate: insOnConflict }));
const insert = vi.fn(() => ({ values: insValues }));

vi.mock("@/lib/db", () => ({
  db: {
    select: (...a: unknown[]) => select(...a),
    insert: (...a: unknown[]) => insert(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));
vi.mock("@/lib/db/schema", () => ({
  accounts: { id: "accounts.id", eoa_address: "accounts.eoa_address" },
  settings: { key: "settings.key", value: "settings.value" },
}));
vi.mock("@/lib/settings-keys", () => ({
  DANGER_PASSPHRASE_HASH_KEY: "danger_passphrase_hash",
  coerceStringSetting: (v: unknown) => (typeof v === "string" ? v : ""),
}));

import { importAccount } from "@/app/accounts/actions";
import { hashPassphrase } from "@/lib/passphrase";

const EOA = "0x" + "ab".repeat(20); // valid 0x+40hex
const PK = "0x" + "cd".repeat(32); // valid 0x+64hex secp256k1 shape
const DANGER = "operator-danger-pass 台灣";
const ENC = "key-encryption-passphrase";

/** A baseline VALID input; individual tests override single fields. */
function baseInput(over: Record<string, unknown> = {}) {
  return {
    eoaAddress: EOA,
    privateKey: PK,
    signatureType: 0,
    funder: "",
    name: "",
    dangerPassphrase: DANGER,
    encryptionPassphrase: ENC,
    ...over,
  };
}

describe("importAccount Server Action", () => {
  beforeEach(() => {
    // Low KDF iters so the REAL hashPassphrase / verifyPassphrase /
    // encryptPrivateKey paths are fast (clamped floor == 100000).
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000";
    select.mockClear();
    selFrom.mockClear();
    selWhere.mockClear();
    selLimit.mockClear().mockResolvedValue([]);
    insert.mockClear();
    insValues.mockClear();
    insOnConflict.mockClear();
    insReturning.mockClear().mockResolvedValue([{ inserted: true }]);
  });

  it("rejects an invalid EOA shape before any DB/crypto work", async () => {
    const res = await importAccount(baseInput({ eoaAddress: "0xnothex" }));
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/EOA 位址格式無效/);
    expect(select).not.toHaveBeenCalled();
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects a malformed private key WITHOUT echoing it", async () => {
    const secret = "0xdeadbeefNOTHEX";
    const res = await importAccount(baseInput({ privateKey: secret }));
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/私鑰格式無效/);
    expect(res.message).not.toContain(secret);
    expect(res.message).not.toContain("deadbeef");
    expect(select).not.toHaveBeenCalled();
  });

  it("rejects a signature_type outside 0..3", async () => {
    const res = await importAccount(baseInput({ signatureType: 4 }));
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/簽章類型無效/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("type 1/2/3 with NO funder is refused (no fabricated funder)", async () => {
    const res = await importAccount(
      baseInput({ signatureType: 2, funder: "" })
    );
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/經鏈上驗證的 funder/);
    // refused at funder-resolution, BEFORE the danger gate / DB
    expect(select).not.toHaveBeenCalled();
    expect(insert).not.toHaveBeenCalled();
  });

  it("fails closed when the danger passphrase hash is not provisioned", async () => {
    selLimit.mockResolvedValueOnce([]); // no settings row
    const res = await importAccount(baseInput());
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/危險操作密碼尚未設定/);
    expect(insert).not.toHaveBeenCalled(); // never wrote the key
  });

  it("rejects a wrong danger passphrase (real constant-time verify)", async () => {
    const stored = await hashPassphrase("the-correct-danger-pass");
    selLimit.mockResolvedValueOnce([{ value: stored }]);
    const res = await importAccount(
      baseInput({ dangerPassphrase: "WRONG" })
    );
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/危險操作確認密碼錯誤/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("type 0 happy path: encrypts, upserts, reports 建立 (created)", async () => {
    const stored = await hashPassphrase(DANGER);
    selLimit.mockResolvedValueOnce([{ value: stored }]);
    insReturning.mockResolvedValueOnce([{ inserted: true }]);

    const res = await importAccount(baseInput());

    expect(res.ok).toBe(true);
    expect(res.action).toBe("created");
    expect(res.message).toMatch(/已建立帳戶/);
    // The raw private key must NEVER appear in the operator-facing message.
    expect(res.message).not.toContain(PK);
    expect(res.message).not.toContain("cdcd");

    // Upsert called with the right shape.
    expect(insert).toHaveBeenCalledTimes(1);
    const values = insValues.mock.calls[0][0] as Record<string, unknown>;
    expect(values.eoa_address).toBe(EOA.toLowerCase());
    expect(values.funder_address).toBe(EOA.toLowerCase()); // type 0 == EOA
    expect(values.signature_type).toBe(0);
    expect(values.status).toBe("ready");
    // The stored key is an encrypted v1 blob, NOT the plaintext key.
    const blob = values.encrypted_private_key as Buffer;
    expect(Buffer.isBuffer(blob) || blob instanceof Uint8Array).toBe(true);
    expect(blob[0]).toBe(0x01); // v1 version byte
    expect(Buffer.from(blob).toString("utf8")).not.toContain(PK);

    // onConflict target = the UNIQUE eoa_address (atomic upsert).
    const conflict = insOnConflict.mock.calls[0][0] as {
      target: unknown;
      set: Record<string, unknown>;
    };
    expect(conflict.target).toBe("accounts.eoa_address");
    expect(conflict.set.status).toBe("ready");
  });

  it("reports 更新 (updated) when the row already existed (xmax != 0)", async () => {
    const stored = await hashPassphrase(DANGER);
    selLimit.mockResolvedValueOnce([{ value: stored }]);
    insReturning.mockResolvedValueOnce([{ inserted: false }]);

    const res = await importAccount(baseInput({ name: "my-acct" }));
    expect(res.ok).toBe(true);
    expect(res.action).toBe("updated");
    expect(res.message).toMatch(/已更新帳戶 my-acct/);
  });

  it("a DB failure during upsert leaks neither blob nor inputs", async () => {
    const stored = await hashPassphrase(DANGER);
    selLimit.mockResolvedValueOnce([{ value: stored }]);
    insReturning.mockRejectedValueOnce(new Error("pg exploded"));

    const res = await importAccount(baseInput());
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/寫入帳戶失敗/);
    expect(res.message).not.toContain(PK);
    expect(res.message).not.toContain("pg exploded");
  });
});
