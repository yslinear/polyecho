/**
 * Danger-tier passphrase verification for the 危險操作二次確認 flow
 * (PRD §5.7 危险 tier) — P0 security Wave-2 web consumer.
 *
 * FROZEN canonical hash string (Python side is authoritative, the operator
 * provisions `settings.danger_passphrase_hash` via the CLI
 * `polyecho account set-danger-passphrase`; the web NEVER writes/derives it):
 *
 *     pbkdf2$<iterations>$<b64_salt>$<b64_hash>
 *
 *   - exactly 4 `$`-separated fields; field 0 is the literal `pbkdf2`
 *   - <iterations> = decimal ASCII of the clamped KDF iteration count
 *     max(POLYECHO_KEY_KDF_ITERATIONS, MIN_KDF_ITERATIONS) where the floor
 *     MIN_KDF_ITERATIONS = 100_000
 *   - <b64_salt>  = standard Base64 (RFC 4648 `+/` alphabet, `=` padding)
 *     of the random 16-byte salt
 *   - <b64_hash>  = standard Base64 of the 32-byte PBKDF2 output
 *   - KDF = PBKDF2-HMAC-SHA256, password = UTF-8 bytes of the passphrase
 *     (NO unicode normalization), dkLen = 32
 *
 * Verification reads the embedded iterations + salt from the stored string
 * and MUST reject any string whose iterations < MIN_KDF_ITERATIONS. The
 * comparison is constant-time via crypto.timingSafeEqual. This module is the
 * sole web verifier; p5-core only EXECUTES rows already in status
 * `confirmed` and does NOT re-verify the passphrase.
 *
 * Node `crypto` mapping to the frozen format (byte-for-byte):
 *   - `pbkdf2(Buffer.from(passphrase, "utf8"), salt, iterations, 32, "sha256")`
 *     == Python `hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt,
 *        iterations, dklen=32)`
 *   - `Buffer.from(salt|hash).toString("base64")` is RFC 4648 standard
 *     Base64 with `+/` and `=` padding == Python `base64.b64encode(...)`
 *   - `Buffer.from(field, "base64")` round-trips the standard alphabet ==
 *     Python `base64.b64decode(...)`
 */

import { pbkdf2, randomBytes, timingSafeEqual } from "node:crypto";

const KEYLEN = 32; // dkLen — 32-byte PBKDF2 output (== Python dklen=32)
const SALTLEN = 16; // 16-byte random salt (== Python os.urandom(16))
const DIGEST = "sha256"; // PBKDF2-HMAC-SHA256
const DEFAULT_ITERATIONS = 600_000;

/**
 * Hard iteration floor — mirrors the Python `MIN_KDF_ITERATIONS = 100_000`.
 * Both the (rarely used, test-only) producer AND the verify path clamp /
 * reject below this value so a downgraded stored string can never confirm.
 */
export const MIN_KDF_ITERATIONS = 100_000;

/**
 * Resolve the KDF iteration count from env, clamped to the security floor:
 * `max(POLYECHO_KEY_KDF_ITERATIONS, MIN_KDF_ITERATIONS)`. Invalid / unset
 * env falls back to the OWASP default (already well above the floor).
 */
export function kdfIterations(): number {
  const raw = process.env.POLYECHO_KEY_KDF_ITERATIONS;
  const n = raw ? Number.parseInt(raw, 10) : NaN;
  const base =
    !Number.isFinite(n) || n < 1 ? DEFAULT_ITERATIONS : n;
  return Math.max(base, MIN_KDF_ITERATIONS);
}

function pbkdf2Async(
  passphrase: string,
  salt: Buffer,
  iterations: number
): Promise<Buffer> {
  // Password = UTF-8 bytes of the passphrase, NO normalization — identical
  // to Python `passphrase.encode()`.
  const pw = Buffer.from(passphrase, "utf8");
  return new Promise((resolve, reject) => {
    pbkdf2(pw, salt, iterations, KEYLEN, DIGEST, (err, derived) => {
      if (err) reject(err);
      else resolve(derived);
    });
  });
}

/**
 * Produce a canonical `pbkdf2$iter$saltb64$hashb64` string.
 *
 * The web NEVER persists a danger-passphrase hash in production (the
 * operator provisions it via the CLI). This helper exists ONLY so tests can
 * synthesise a Python-format string to exercise the verify path. It still
 * clamps iterations to the MIN_KDF_ITERATIONS floor so a test can never
 * accidentally mint a sub-floor string that the verifier must reject.
 */
export async function hashPassphrase(passphrase: string): Promise<string> {
  const iterations = kdfIterations(); // already clamped to the floor
  const salt = randomBytes(SALTLEN);
  const derived = await pbkdf2Async(passphrase, salt, iterations);
  // toString("base64") == RFC 4648 standard Base64 (`+/`, `=` padding) ==
  // Python base64.b64encode(...).decode().
  return `pbkdf2$${iterations}$${salt.toString("base64")}$${derived.toString(
    "base64"
  )}`;
}

/**
 * Constant-time verify against a FROZEN canonical stored string. Returns
 * false (never throws) on ANY malformed / downgraded stored value so a
 * corrupt or sub-floor row can never be confirmed by accident:
 *
 *   - not exactly 4 `$`-fields, or field 0 != "pbkdf2"            -> false
 *   - iterations not a finite integer, or < MIN_KDF_ITERATIONS    -> false
 *     (the frozen iteration-floor rejection — tampered/downgraded)
 *   - salt / hash not decodable standard Base64                   -> false
 *   - decoded salt empty, or decoded hash != 32 bytes             -> false
 *   - derived != stored                                           -> false
 */
export async function verifyPassphrase(
  passphrase: string,
  stored: string | null | undefined
): Promise<boolean> {
  if (!stored) return false;
  const parts = stored.split("$");
  if (parts.length !== 4 || parts[0] !== "pbkdf2") return false;

  const iterations = Number.parseInt(parts[1], 10);
  if (!Number.isFinite(iterations) || !Number.isInteger(iterations)) {
    return false;
  }
  // rev-web R17 MEDIUM: `parseInt("100000.9", 10)` silently yields 100000 and
  // would PASS the floor check, accepting a non-canonical stored string. The
  // FROZEN contract's <iterations> field is a DECIMAL INTEGER with no sign /
  // leading zeros / fraction / whitespace. Require the parsed value to
  // round-trip EXACTLY back to the raw field; anything else (e.g. "100000.9",
  // "1e5", " 100000", "+100000", "0xF4240") is rejected. This keeps
  // byte-for-byte parity with the Python `int(parts[1])` canonical contract
  // (Python `int("100000.9")` raises -> verify fails) while Node's lenient
  // parseInt does not.
  if (String(iterations) !== parts[1]) return false;
  // FROZEN iteration-floor rejection: reject any string whose embedded
  // iteration count is below the MIN_KDF_ITERATIONS security floor.
  if (iterations < MIN_KDF_ITERATIONS) return false;

  let salt: Buffer;
  let expected: Buffer;
  try {
    salt = Buffer.from(parts[2], "base64");
    expected = Buffer.from(parts[3], "base64");
  } catch {
    return false;
  }
  if (salt.length === 0 || expected.length !== KEYLEN) return false;

  const actual = await pbkdf2Async(passphrase, salt, iterations);
  if (actual.length !== expected.length) return false;
  return timingSafeEqual(actual, expected);
}

/**
 * Operator-provisioned canonical danger-passphrase hash, or `""` when the
 * row has never been written (the consumer MUST treat empty as "not
 * provisioned" — never auto-provision). Centralises the
 * `settings.danger_passphrase_hash` → coerce → trim flow that previously
 * lived in three call sites (inline-confirm-gate.ts, alerts/actions.ts,
 * accounts/actions.ts) byte-for-byte.
 *
 * DB imports are lazy so this module stays import-safe for pure-crypto
 * tests (`__tests__/lib/passphrase.test.ts`, `key-crypto.test.ts`) that do
 * NOT mock `@/lib/db` / `@/lib/env`.
 */
export async function loadDangerPassphraseHash(): Promise<string> {
  const [{ db }, { settings }, { eq }, settingsKeys] = await Promise.all([
    import("@/lib/db"),
    import("@/lib/db/schema"),
    import("drizzle-orm"),
    import("@/lib/settings-keys"),
  ]);
  const [hashRow] = await db
    .select({ value: settings.value })
    .from(settings)
    .where(eq(settings.key, settingsKeys.DANGER_PASSPHRASE_HASH_KEY))
    .limit(1);
  return settingsKeys.coerceStringSetting(hashRow?.value).trim();
}
