/**
 * Passphrase hashing for the 危險操作二次確認 flow (PRD §5.7 危险 tier).
 *
 * Self-describing hash format stored in pending_actions.confirm_passphrase_hash:
 *
 *     pbkdf2$<iterations>$<saltBase64>$<hashBase64>
 *
 * so verification needs nothing but the stored string (the iteration count
 * and salt travel with it — future-proof if POLYECHO_KEY_KDF_ITERATIONS
 * changes between create and confirm).
 *
 * Algorithm: PBKDF2-HMAC-SHA256, 32-byte derived key, per-row 16-byte random
 * salt. Iterations come from env POLYECHO_KEY_KDF_ITERATIONS (default 600000).
 * Verification is constant-time via crypto.timingSafeEqual.
 *
 * WEB OWNS VERIFICATION. p5-core only ever EXECUTES rows already in status
 * `confirmed` and does NOT re-verify the passphrase. The Python
 * `shared.pending_actions` helpers are for CLI/Python-initiated flows only;
 * this module is the sole verifier for web-initiated dangerous actions.
 */

import { pbkdf2, randomBytes, timingSafeEqual } from "node:crypto";

const KEYLEN = 32;
const DIGEST = "sha256";
const DEFAULT_ITERATIONS = 600_000;

/** Resolve the KDF iteration count from env, with a safe floor. */
export function kdfIterations(): number {
  const raw = process.env.POLYECHO_KEY_KDF_ITERATIONS;
  const n = raw ? Number.parseInt(raw, 10) : NaN;
  if (!Number.isFinite(n) || n < 1) return DEFAULT_ITERATIONS;
  return n;
}

function pbkdf2Async(
  passphrase: string,
  salt: Buffer,
  iterations: number
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    pbkdf2(passphrase, salt, iterations, KEYLEN, DIGEST, (err, derived) => {
      if (err) reject(err);
      else resolve(derived);
    });
  });
}

/** Hash a passphrase into the self-describing `pbkdf2$iter$salt$hash` string. */
export async function hashPassphrase(passphrase: string): Promise<string> {
  const iterations = kdfIterations();
  const salt = randomBytes(16);
  const derived = await pbkdf2Async(passphrase, salt, iterations);
  return `pbkdf2$${iterations}$${salt.toString("base64")}$${derived.toString(
    "base64"
  )}`;
}

/**
 * Constant-time verify. Returns false (never throws) on any malformed stored
 * value so a corrupt row can never be confirmed by accident.
 */
export async function verifyPassphrase(
  passphrase: string,
  stored: string | null | undefined
): Promise<boolean> {
  if (!stored) return false;
  const parts = stored.split("$");
  if (parts.length !== 4 || parts[0] !== "pbkdf2") return false;
  const iterations = Number.parseInt(parts[1], 10);
  if (!Number.isFinite(iterations) || iterations < 1) return false;

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
