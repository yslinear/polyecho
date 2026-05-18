import { describe, it, expect, beforeEach } from "vitest";

/**
 * lib/passphrase — PBKDF2-HMAC-SHA256 verify for §5.7 危险-tier confirm,
 * P0 security Wave-2.
 *
 * Contract (mirrors the FROZEN Python canonical string byte-for-byte):
 *  - hashPassphrase emits `pbkdf2$<iter>$<saltb64>$<hashb64>` (standard
 *    Base64, 16-byte salt, 32-byte SHA256 PBKDF2 output, UTF-8 password)
 *  - iterations are clamped to max(POLYECHO_KEY_KDF_ITERATIONS,
 *    MIN_KDF_ITERATIONS=100000) for BOTH produce and the verify floor
 *  - verifyPassphrase round-trips correct, rejects wrong, NEVER throws on
 *    malformed input, stays verifiable if env iterations change later, and
 *    REJECTS any stored string whose embedded iterations < 100000
 *  - verifies a hash produced in the exact Python format (cross-impl)
 */

beforeEach(() => {
  // Above the floor so tests stay fast but exercise the real clamp.
  process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000";
});

import {
  hashPassphrase,
  verifyPassphrase,
  kdfIterations,
  MIN_KDF_ITERATIONS,
} from "@/lib/passphrase";
import { pbkdf2Sync, randomBytes } from "node:crypto";

describe("kdfIterations (clamped to the MIN_KDF_ITERATIONS floor)", () => {
  it("exposes the frozen 100000 floor", () => {
    expect(MIN_KDF_ITERATIONS).toBe(100_000);
  });

  it("clamps an env value below the floor UP to 100000", () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "1000";
    expect(kdfIterations()).toBe(100_000);
  });

  it("passes through an env value above the floor", () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "250000";
    expect(kdfIterations()).toBe(250_000);
  });

  it("defaults to 600000 when unset or invalid (still >= floor)", () => {
    delete process.env.POLYECHO_KEY_KDF_ITERATIONS;
    expect(kdfIterations()).toBe(600_000);
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "-5";
    expect(kdfIterations()).toBe(600_000);
  });
});

describe("hashPassphrase / verifyPassphrase", () => {
  it("produces the canonical pbkdf2$iter$salt$hash format (clamped iter)", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "1000"; // below floor
    const h = await hashPassphrase("super secret");
    const parts = h.split("$");
    expect(parts).toHaveLength(4);
    expect(parts[0]).toBe("pbkdf2");
    expect(parts[1]).toBe("100000"); // clamped UP to the floor
    expect(Buffer.from(parts[2], "base64").length).toBe(16);
    expect(Buffer.from(parts[3], "base64").length).toBe(32);
  });

  it("verifies the correct passphrase and rejects the wrong one", async () => {
    const h = await hashPassphrase("correct horse battery");
    expect(await verifyPassphrase("correct horse battery", h)).toBe(true);
    expect(await verifyPassphrase("wrong", h)).toBe(false);
  });

  it("uses a fresh random salt per hash (two hashes differ)", async () => {
    const a = await hashPassphrase("same");
    const b = await hashPassphrase("same");
    expect(a).not.toBe(b);
    expect(await verifyPassphrase("same", a)).toBe(true);
    expect(await verifyPassphrase("same", b)).toBe(true);
  });

  it("stays verifiable when the env iteration count changes afterwards", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "120000";
    const h = await hashPassphrase("pass");
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "999999"; // changed after create
    expect(await verifyPassphrase("pass", h)).toBe(true);
  });

  it("verifies a string produced in the EXACT Python canonical format", async () => {
    // Reproduce the frozen Python producer with Node primitives:
    //   salt = os.urandom(16)
    //   dk   = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters, 32)
    //   "pbkdf2$%d$%s$%s" % (iters, b64(salt), b64(dk))
    const iters = 100_000;
    const salt = randomBytes(16);
    const pw = "台灣繁體 pass-測試"; // non-ASCII -> UTF-8, no normalization
    const dk = pbkdf2Sync(Buffer.from(pw, "utf8"), salt, iters, 32, "sha256");
    const pythonStr = `pbkdf2$${iters}$${salt.toString(
      "base64"
    )}$${dk.toString("base64")}`;
    expect(await verifyPassphrase(pw, pythonStr)).toBe(true);
    expect(await verifyPassphrase("not it", pythonStr)).toBe(false);
  });

  it("REJECTS a tampered string with iterations below the 100000 floor", async () => {
    // Valid material but iteration field downgraded below the floor.
    const salt = randomBytes(16);
    const pw = "downgrade me";
    const dk = pbkdf2Sync(Buffer.from(pw, "utf8"), salt, 99_999, 32, "sha256");
    const belowFloor = `pbkdf2$99999$${salt.toString(
      "base64"
    )}$${dk.toString("base64")}`;
    // Even the CORRECT passphrase must be rejected (sub-floor string).
    expect(await verifyPassphrase(pw, belowFloor)).toBe(false);
  });

  it("REJECTS a non-canonical float-format iterations field (R17 MEDIUM)", async () => {
    // `parseInt("100000.9", 10)` silently yields 100000 and would PASS the
    // floor — but the FROZEN contract's <iterations> is a decimal integer.
    // Python `int("100000.9")` raises, so verify must fail here too. Build
    // material that hashes correctly at 100000 iters, then corrupt ONLY the
    // iterations field to a float-format string.
    const salt = randomBytes(16);
    const pw = "float iters";
    const dk = pbkdf2Sync(
      Buffer.from(pw, "utf8"),
      salt,
      100_000,
      32,
      "sha256"
    );
    const b64 = `${salt.toString("base64")}$${dk.toString("base64")}`;
    // Even the CORRECT passphrase must be rejected — the field is not the
    // canonical decimal-integer shape.
    expect(await verifyPassphrase(pw, `pbkdf2$100000.9$${b64}`)).toBe(false);
    expect(await verifyPassphrase(pw, `pbkdf2$100000.0$${b64}`)).toBe(false);
    expect(await verifyPassphrase(pw, `pbkdf2$ 100000$${b64}`)).toBe(false);
    expect(await verifyPassphrase(pw, `pbkdf2$+100000$${b64}`)).toBe(false);
    expect(await verifyPassphrase(pw, `pbkdf2$1e5$${b64}`)).toBe(false);
    // The exact canonical decimal-integer form still verifies.
    expect(await verifyPassphrase(pw, `pbkdf2$100000$${b64}`)).toBe(true);
  });

  it("never throws and returns false on malformed/empty stored values", async () => {
    expect(await verifyPassphrase("x", null)).toBe(false);
    expect(await verifyPassphrase("x", "")).toBe(false);
    expect(await verifyPassphrase("x", "not-a-hash")).toBe(false);
    expect(await verifyPassphrase("x", "pbkdf2$abc$salt$hash")).toBe(false);
    expect(await verifyPassphrase("x", "scrypt$100000$s$h")).toBe(false);
    expect(await verifyPassphrase("x", "pbkdf2$100000$$=")).toBe(false);
    // Below the floor -> false even though structurally well-formed.
    expect(await verifyPassphrase("x", "pbkdf2$50000$AAAA$AAAA")).toBe(false);
  });
});
