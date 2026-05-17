import { describe, it, expect, beforeEach } from "vitest";

/**
 * lib/passphrase — PBKDF2-HMAC-SHA256 for §5.7 危险-tier confirm.
 *
 * Contract:
 *  - hashPassphrase emits self-describing `pbkdf2$<iter>$<saltb64>$<hashb64>`
 *  - the iteration count comes from POLYECHO_KEY_KDF_ITERATIONS (default 600k)
 *  - verifyPassphrase round-trips correct, rejects wrong, never throws on
 *    malformed input, and stays verifiable even if the env iteration count
 *    changes (the count is read back from the stored string).
 */

beforeEach(() => {
  process.env.POLYECHO_KEY_KDF_ITERATIONS = "1500";
});

import {
  hashPassphrase,
  verifyPassphrase,
  kdfIterations,
} from "@/lib/passphrase";

describe("kdfIterations", () => {
  it("reads the env value", () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "777";
    expect(kdfIterations()).toBe(777);
  });

  it("defaults to 600000 when unset or invalid", () => {
    delete process.env.POLYECHO_KEY_KDF_ITERATIONS;
    expect(kdfIterations()).toBe(600_000);
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "-5";
    expect(kdfIterations()).toBe(600_000);
  });
});

describe("hashPassphrase / verifyPassphrase", () => {
  it("produces the self-describing pbkdf2$iter$salt$hash format", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "1500";
    const h = await hashPassphrase("super secret");
    const parts = h.split("$");
    expect(parts).toHaveLength(4);
    expect(parts[0]).toBe("pbkdf2");
    expect(parts[1]).toBe("1500");
    // base64 salt + base64 hash, decodable.
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
    // ...yet both still verify (proves salt is embedded + used).
    expect(await verifyPassphrase("same", a)).toBe(true);
    expect(await verifyPassphrase("same", b)).toBe(true);
  });

  it("stays verifiable when the env iteration count changes afterwards", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "1200";
    const h = await hashPassphrase("pass");
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "9999"; // changed after create
    expect(await verifyPassphrase("pass", h)).toBe(true);
  });

  it("never throws and returns false on malformed/empty stored values", async () => {
    expect(await verifyPassphrase("x", null)).toBe(false);
    expect(await verifyPassphrase("x", "")).toBe(false);
    expect(await verifyPassphrase("x", "not-a-hash")).toBe(false);
    expect(await verifyPassphrase("x", "pbkdf2$abc$salt$hash")).toBe(false);
    expect(await verifyPassphrase("x", "scrypt$1$s$h")).toBe(false);
    expect(await verifyPassphrase("x", "pbkdf2$1000$$=")).toBe(false);
  });
});
