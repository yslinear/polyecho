/**
 * Private-key AES-256-GCM encryptor — the TS byte-mirror of the FROZEN
 * Python `polyecho.shared.crypto.encrypt_private_key` (blob **v1**).
 *
 * WHY THIS EXISTS
 * ---------------
 * The CLI-only key-isolation model has been EXPLICITLY + knowingly overridden
 * by the operator (recorded) so the web `/accounts` page can import a private
 * key. trader-core later decrypts `accounts.encrypted_private_key` ONLY via
 * the frozen Python `decrypt_private_key`. Therefore the blob this module
 * writes MUST be byte-identical to what Python `encrypt_private_key`
 * produces, or trader-core silently cannot use the key.
 *
 * FROZEN v1 BLOB LAYOUT (authoritative spec: packages/shared/src/polyecho/
 * shared/crypto.py `encrypt_private_key` — read its CODE, not its docstring
 * diagram, which uses a tag-before-ciphertext shorthand the code does not):
 *
 *     [ 0x01 (1 B) | iterations (4 B, big-endian uint32)
 *       | salt (32 B) | nonce (12 B) | ciphertext (N B) | tag (16 B) ]
 *
 *   - The 5-byte header `version || iterations` is bound as AES-GCM
 *     ASSOCIATED DATA (AAD) so a flipped iteration count fails the GCM tag
 *     instead of silently mis-deriving the key. Python:
 *       `header = bytes([0x01]) + struct.pack(">I", iterations)`
 *       `ct_and_tag = aesgcm.encrypt(nonce, plaintext.encode(), header)`
 *       `return header + salt + nonce + ct_and_tag`
 *     Python's `cryptography` AESGCM APPENDS the 16-byte tag to the
 *     ciphertext (`ct_and_tag` == ciphertext‖tag); Node's GCM cipher keeps
 *     them separate (`getAuthTag()`). To be byte-identical this module
 *     emits the tag LAST — `header || salt || nonce || ciphertext || tag` —
 *     exactly Python's `header + salt + nonce + (ciphertext + tag)`.
 *
 * BYTE-FOR-BYTE Node <-> Python mapping (verified against crypto.py):
 *   - KDF: `pbkdf2(Buffer.from(passphrase,"utf8"), salt, iterations, 32,
 *     "sha256")` == Python `PBKDF2HMAC(SHA256, length=32, salt, iterations)
 *     .derive(passphrase.encode())`. Password = raw UTF-8 bytes, NO unicode
 *     normalization (identical to Python `passphrase.encode()`).
 *   - `randomBytes(32)` salt / `randomBytes(12)` nonce == Python
 *     `os.urandom(32)` / `os.urandom(12)` (CSPRNG; sizes frozen).
 *   - Header big-endian uint32: `buf.writeUInt32BE(iterations, 1)` ==
 *     Python `struct.pack(">I", iterations)`.
 *   - AAD: `cipher.setAAD(header)` (header = the exact 5 bytes) == Python's
 *     `aesgcm.encrypt(nonce, pt, header)` associated-data argument.
 *   - Cipher: AES-256-GCM, 12-byte nonce, 16-byte tag (`getAuthTag()`).
 *   - iterations = the CLAMPED `max(POLYECHO_KEY_KDF_ITERATIONS,
 *     MIN_KDF_ITERATIONS=100000)` (default 600000) — reused VERBATIM from
 *     `lib/passphrase.ts` `kdfIterations()` so the web has ONE clamp
 *     implementation and can never drift from the danger-passphrase floor or
 *     Python's `max(int(settings.key_kdf_iterations), MIN_KDF_ITERATIONS)`.
 *
 * SECURITY: the private key, passphrase, and derived AES key are NEVER
 * logged, thrown, or placed in an Error message. The only thrown errors are
 * generic input-shape rejections that contain NO secret material.
 */

import {
  pbkdf2,
  randomBytes,
  createCipheriv,
  type CipherGCM,
} from "node:crypto";

import { kdfIterations } from "@/lib/passphrase";

// --- Frozen field sizes (changing ANY breaks existing blobs) --------------
/** v1 blob version byte (== Python `_BLOB_VERSION_V1 = 0x01`). */
const BLOB_VERSION_V1 = 0x01;
/** 1-byte version || 4-byte BE uint32 iterations (== Python `_V1_HEADER_SIZE`). */
const V1_HEADER_SIZE = 5;
/** Random salt length (== Python `_SALT_SIZE = 32`, `os.urandom(32)`). */
const SALT_SIZE = 32;
/** Random GCM nonce length (== Python `_NONCE_SIZE = 12`, `os.urandom(12)`). */
const NONCE_SIZE = 12;
/** GCM auth tag length (== Python `_TAG_SIZE = 16`). */
const TAG_SIZE = 16;
/** Derived AES-256 key length (== Python `_KEY_SIZE = 32`). */
const KEY_SIZE = 32;
/** Largest value the 4-byte BE iteration field can hold (== Python `_MAX_ITERATIONS`). */
const MAX_ITERATIONS = 0xffffffff;
const DIGEST = "sha256"; // PBKDF2-HMAC-SHA256 (== Python SHA256())

/**
 * Derive the 32-byte AES key — PBKDF2-HMAC-SHA256 over the UTF-8 passphrase
 * bytes (NO normalization), byte-identical to Python `_derive_key`.
 */
function deriveKey(
  passphrase: string,
  salt: Buffer,
  iterations: number
): Promise<Buffer> {
  // Password = raw UTF-8 bytes, NO unicode normalization == Python
  // `passphrase.encode()`.
  const pw = Buffer.from(passphrase, "utf8");
  return new Promise((resolve, reject) => {
    pbkdf2(pw, salt, iterations, KEY_SIZE, DIGEST, (err, derived) => {
      if (err) reject(err);
      else resolve(derived);
    });
  });
}

/**
 * Encrypt `privateKeyHex` with AES-256-GCM, producing the EXACT Python v1
 * blob: `0x01 || iterations(4B BE) || salt(32B) || nonce(12B) ||
 * ciphertext || tag(16B)`. The 5-byte header is AES-GCM associated data.
 *
 * The returned Buffer is byte-identical to Python
 * `encrypt_private_key(privateKeyHex, passphrase)` for the same inputs +
 * the same effective iteration count, so trader-core's frozen
 * `decrypt_private_key` recovers the key exactly.
 *
 * @param privateKeyHex The private key string to encrypt (e.g. "0x…64hex").
 *                       Stored as its raw UTF-8 bytes — Python encrypts
 *                       `plaintext.encode()`, so we encrypt the same bytes.
 * @param passphrase     The user-supplied encryption passphrase.
 * @throws If either input is empty (generic message — NEVER echoes secrets),
 *         mirroring Python's `ValueError("plaintext/passphrase must not be
 *         empty")`.
 */
export async function encryptPrivateKey(
  privateKeyHex: string,
  passphrase: string
): Promise<Buffer> {
  // Mirror Python's empty-input ValueErrors. Messages carry NO secret.
  if (!privateKeyHex) {
    throw new Error("plaintext must not be empty");
  }
  if (!passphrase) {
    throw new Error("passphrase must not be empty");
  }

  // Python: iterations = max(int(settings.key_kdf_iterations),
  //                          MIN_KDF_ITERATIONS).  `kdfIterations()` ALREADY
  //  applies exactly that clamp (returns max(env, MIN_KDF_ITERATIONS),
  //  shared with the danger-passphrase verifier), so re-flooring here would
  //  be dead code — the web has a SINGLE clamp source of truth.
  const iterations = kdfIterations();
  if (iterations > MAX_ITERATIONS) {
    // Mirrors Python's "exceeds the storable maximum" guard. The 600000
    // default is ≪ 2**32; this is an absurd-config defence only.
    throw new Error(
      `key_kdf_iterations ${iterations} exceeds the storable maximum ${MAX_ITERATIONS}`
    );
  }

  const salt = randomBytes(SALT_SIZE); // == os.urandom(32)
  const nonce = randomBytes(NONCE_SIZE); // == os.urandom(12)
  const key = await deriveKey(passphrase, salt, iterations);

  // Header = version || BE-uint32 iterations. Bound as AES-GCM AAD so the
  // stored iteration count is authenticated (tamper => InvalidTag, NOT a
  // silent wrong-key derivation). Byte-identical to Python
  // `bytes([0x01]) + struct.pack(">I", iterations)`.
  const header = Buffer.alloc(V1_HEADER_SIZE);
  header.writeUInt8(BLOB_VERSION_V1, 0);
  header.writeUInt32BE(iterations, 1); // ">I" big-endian uint32

  // Node's GCM cipher keeps ciphertext and tag separate; Python's
  // `cryptography` AESGCM.encrypt returns `ciphertext + tag` (tag APPENDED).
  // To be byte-identical we emit `... nonce | ciphertext | tag`.
  const cipher = createCipheriv("aes-256-gcm", key, nonce, {
    authTagLength: TAG_SIZE,
  }) as CipherGCM;
  // AAD MUST be set before any update() — exactly the 5 header bytes.
  // (No `plaintextLength` option: that is a CCM-only hint; GCM does not
  // need it and Python's AESGCM binds the AAD with no length prefix.)
  cipher.setAAD(header);

  // Python encrypts `plaintext.encode()` (UTF-8). Encrypt the same bytes.
  const pt = Buffer.from(privateKeyHex, "utf8");
  const ciphertext = Buffer.concat([cipher.update(pt), cipher.final()]);
  const tag = cipher.getAuthTag(); // 16 bytes (authTagLength pinned above)

  // Final wire order == Python `header + salt + nonce + (ciphertext + tag)`.
  return Buffer.concat([header, salt, nonce, ciphertext, tag]);
}
