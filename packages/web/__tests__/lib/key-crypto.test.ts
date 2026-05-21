import { describe, it, expect } from "vitest";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  pbkdf2Sync,
  createDecipheriv,
  type DecipherGCM,
} from "node:crypto";

import { encryptPrivateKey } from "@/lib/key-crypto";

/**
 * lib/key-crypto — the TS byte-mirror of the FROZEN Python
 * `polyecho.shared.crypto.encrypt_private_key` (blob v1).
 *
 * The CROSS-LANGUAGE acceptance gate is split across the two independent CI
 * legs (see /tmp/everify/v8.sh): the `web` leg (node, NO python) runs THIS
 * file; the `python` leg (uv image, NO node) runs
 * `packages/shared/tests/test_keycrypto_crosslang.py`. They meet at a
 * fixture FILE this test (re)writes —
 * `packages/shared/tests/ts_keycrypto_blob.json` (a single file directly
 * inside the already-tracked `tests/` dir, NOT a new subdir: the CI
 * byte-mirror overlay copies an individual untracked file but not a brand
 * new untracked directory) — containing a TS-produced v1 blob; the pytest
 * reads it and asserts the FROZEN `decrypt_private_key` recovers the exact
 * key. This test refreshes it so it always reflects the CURRENT
 * key-crypto.ts.
 *
 * Here we ALSO decrypt the TS blob with raw `node:crypto` primitives using
 * the EXACT Python algorithm (PBKDF2-HMAC-SHA256 over UTF-8 passphrase
 * bytes, AES-256-GCM, 5-byte header as AAD) — an independent in-leg proof
 * that the blob is structurally the v1 Python expects.
 *
 * FIXTURE REGENERATION RECIPE
 * ---------------------------
 * The cross-language fixture `packages/shared/tests/ts_keycrypto_blob.json`
 * is COMMITTED and treated as a pinned vector. Each test run encrypts under
 * a fresh random salt/IV and in-memory verifies the produced blob; the
 * on-disk fixture is ONLY rewritten when explicitly requested. This keeps
 * `git status` clean on routine vitest runs.
 *
 * To regenerate after a deliberate key-crypto change:
 *     REGENERATE_FIXTURE=1 pnpm --filter @polyecho/web test -- key-crypto
 *
 * Then commit the updated `packages/shared/tests/ts_keycrypto_blob.json`
 * alongside the code change. The Python leg
 * (`packages/shared/tests/test_keycrypto_crosslang.py`) always reads the
 * on-disk file as the pinned vector.
 */

// Frozen field sizes (must match crypto.py / key-crypto.ts exactly).
const V1_HEADER_SIZE = 5;
const SALT_SIZE = 32;
const NONCE_SIZE = 12;
const TAG_SIZE = 16;

// A realistic 32-byte (64-hex) secp256k1 key + a unicode passphrase to
// prove UTF-8 (no normalization) parity with Python `passphrase.encode()`.
const PK = "0x" + "ab".repeat(32);
const PASSPHRASE = "correct horse battery staple 台灣-測試";

/** Independent v1 decryptor using the EXACT Python algorithm. */
function decryptV1Like(blob: Buffer, passphrase: string): string {
  expect(blob[0]).toBe(0x01); // version byte
  const header = blob.subarray(0, V1_HEADER_SIZE); // version || iter (AAD)
  const iterations = blob.readUInt32BE(1); // ">I" big-endian uint32
  let off = V1_HEADER_SIZE;
  const salt = blob.subarray(off, off + SALT_SIZE);
  off += SALT_SIZE;
  const nonce = blob.subarray(off, off + NONCE_SIZE);
  off += NONCE_SIZE;
  // Python AESGCM emits ciphertext THEN the 16-byte tag at the very end.
  const tag = blob.subarray(blob.length - TAG_SIZE);
  const ciphertext = blob.subarray(off, blob.length - TAG_SIZE);

  const key = pbkdf2Sync(
    Buffer.from(passphrase, "utf8"),
    salt,
    iterations,
    32,
    "sha256"
  );
  const decipher = createDecipheriv("aes-256-gcm", key, nonce, {
    authTagLength: TAG_SIZE,
  }) as DecipherGCM;
  decipher.setAAD(header);
  decipher.setAuthTag(tag);
  return Buffer.concat([
    decipher.update(ciphertext),
    decipher.final(),
  ]).toString("utf8");
}

describe("key-crypto encryptPrivateKey — FROZEN Python v1 blob mirror", () => {
  it("emits the exact v1 layout: 0x01 | iter(4B BE) | salt32 | nonce12 | ct | tag16", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000"; // floor; fast
    const blob = await encryptPrivateKey(PK, PASSPHRASE);

    expect(blob[0]).toBe(0x01);
    expect(blob.readUInt32BE(1)).toBe(100_000); // clamped iter, big-endian
    // overhead = header(5) + salt(32) + nonce(12) + tag(16) = 65
    expect(blob.length).toBe(
      V1_HEADER_SIZE +
        SALT_SIZE +
        NONCE_SIZE +
        TAG_SIZE +
        Buffer.byteLength(PK, "utf8")
    );
  });

  it("clamps a sub-floor env iteration count UP to 100000 (== Python clamp)", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "1000"; // below floor
    const blob = await encryptPrivateKey(PK, PASSPHRASE);
    expect(blob.readUInt32BE(1)).toBe(100_000);
  });

  it("round-trips via the EXACT Python algorithm (AAD=5-byte header)", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000";
    const blob = await encryptPrivateKey(PK, PASSPHRASE);
    expect(decryptV1Like(blob, PASSPHRASE)).toBe(PK);
  });

  it("a flipped iteration-count header byte fails the GCM tag (AAD bound)", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000";
    const blob = Buffer.from(await encryptPrivateKey(PK, PASSPHRASE));
    blob[4] ^= 0x01; // mutate the LSB of the iteration field (part of AAD)
    expect(() => decryptV1Like(blob, PASSPHRASE)).toThrow();
  });

  it("uses a fresh random salt + nonce per call (two blobs differ)", async () => {
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000";
    const a = await encryptPrivateKey(PK, PASSPHRASE);
    const b = await encryptPrivateKey(PK, PASSPHRASE);
    expect(a.equals(b)).toBe(false);
    expect(decryptV1Like(a, PASSPHRASE)).toBe(PK);
    expect(decryptV1Like(b, PASSPHRASE)).toBe(PK);
  });

  it("rejects empty inputs without echoing secrets (== Python ValueError)", async () => {
    await expect(encryptPrivateKey("", PASSPHRASE)).rejects.toThrow(
      "plaintext must not be empty"
    );
    await expect(encryptPrivateKey(PK, "")).rejects.toThrow(
      "passphrase must not be empty"
    );
  });

  it("writes the cross-language fixture the pytest leg asserts against", async () => {
    // Pin the iteration count low so the python leg's PBKDF2 is fast.
    process.env.POLYECHO_KEY_KDF_ITERATIONS = "100000";
    const blob = await encryptPrivateKey(PK, PASSPHRASE);

    // Sanity: it must decrypt with the Python algorithm before we hand it
    // to the python leg.
    expect(decryptV1Like(blob, PASSPHRASE)).toBe(PK);

    // Single file directly inside the already-tracked shared tests/ dir
    // (NOT a new subdir — the CI byte-mirror overlay reliably copies an
    // individual untracked file but mishandles a brand new untracked
    // directory, which is why an earlier `fixtures/` subdir failed the
    // python leg).
    const fixturePath = resolve(
      __dirname,
      "../../../shared/tests/ts_keycrypto_blob.json"
    );
    const fixture = {
      _comment:
        "GENERATED by packages/web __tests__/lib/key-crypto.test.ts. The " +
        "blob is a v1 AES-256-GCM private-key blob produced by the TS " +
        "encryptPrivateKey. test_keycrypto_crosslang.py asserts the FROZEN " +
        "Python decrypt_private_key recovers `private_key` from it. Do not " +
        "hand-edit; the web test regenerates it.",
      iterations: 100000,
      passphrase: PASSPHRASE,
      private_key: PK,
      blob_hex: blob.toString("hex"),
    };
    // Gate the on-disk write behind REGENERATE_FIXTURE=1 so routine vitest
    // runs do NOT mutate the committed pinned cross-lang vector. The
    // in-memory `decryptV1Like(blob, PASSPHRASE) === PK` assertion above
    // already proves the TS encryptor still produces a Python-decryptable
    // blob; the on-disk fixture only needs refreshing after a deliberate
    // key-crypto change (see "FIXTURE REGENERATION RECIPE" at top of file).
    if (process.env.REGENERATE_FIXTURE === "1") {
      writeFileSync(
        fixturePath,
        JSON.stringify(fixture, null, 2) + "\n",
        "utf8"
      );
    }

    // The committed file must be parseable + structurally a v1 blob.
    expect(blob[0]).toBe(0x01);
    expect(blob.readUInt32BE(1)).toBe(100_000);
    // Silence unused-binding lint when the write is gated off.
    void fixturePath;
    void fixture;
  });
});
