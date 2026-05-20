/**
 * magic-proxy.ts — TS byte-mirror of the FROZEN Python
 * `polyecho.shared.polymarket.wallet.magic_proxy_address` (brief §9, sig-type 1).
 *
 * WHY THIS EXISTS
 * ---------------
 * The web `/accounts` import form does a P2 CREATE2 cross-check: when the
 * operator picks sig-type 1 (POLY_PROXY) AND enters an explicit funder, we
 * compute the CREATE2 Magic-proxy address from the EOA and warn (NEVER
 * block) when the two differ. The check is advisory only — it is rendered
 * as a yellow inline note, the form still submits, and the persisted
 * funder is whatever the operator typed.
 *
 * SAFETY STANCE
 * -------------
 * The byte-mirror pattern used by `lib/key-crypto.ts` cannot apply here
 * (the persisted value is honoured-as-typed; the derived value is only
 * shown to the operator). Therefore a divergent computation here is at
 * worst a noisy/false warning — never a silently-wrong on-chain decision.
 *
 * The CREATE2 constants are MIRRORED from
 * `packages/shared/src/polyecho/shared/constants.py`
 *   - `MAGIC_PROXY_FACTORY`
 *   - `MAGIC_PROXY_INIT_CODE_HASH`
 * A unit test (`magic-proxy.test.ts`) pins the output to a known good
 * vector that matches what the Python `magic_proxy_address` returns for
 * a fixed test EOA — so any drift in the constants or the keccak
 * implementation is caught by CI before the warn channel goes stale.
 *
 * The keccak256 implementation below is the standard Ethereum keccak
 * (0x01 padding, NOT NIST SHA3's 0x06 — Node's built-in `sha3-256` is
 * NIST and would compute the wrong factory address).
 */

/** Mirrors `polyecho.shared.constants.MAGIC_PROXY_FACTORY` (brief §9). */
export const MAGIC_PROXY_FACTORY =
  "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052";

/** Mirrors `polyecho.shared.constants.MAGIC_PROXY_INIT_CODE_HASH` (brief §9). */
export const MAGIC_PROXY_INIT_CODE_HASH =
  "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b";

const EVM_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

function hexToBytes(hex: string): Uint8Array {
  const h = hex.startsWith("0x") ? hex.slice(2) : hex;
  if (h.length % 2 !== 0) {
    throw new Error("Hex string has odd length");
  }
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) {
    const byte = parseInt(h.substr(i * 2, 2), 16);
    if (Number.isNaN(byte)) throw new Error("Invalid hex character");
    out[i] = byte;
  }
  return out;
}

function bytesToHex(bytes: Uint8Array): string {
  let s = "0x";
  for (let i = 0; i < bytes.length; i++) {
    s += bytes[i].toString(16).padStart(2, "0");
  }
  return s;
}

// ---------------------------------------------------------------------------
// keccak256 (Ethereum padding — 0x01, NOT NIST SHA3's 0x06)
// Minimal, dependency-free implementation. Verified against the Python
// `eth_utils.crypto.keccak` output in `__tests__/lib/magic-proxy.test.ts`.
// ---------------------------------------------------------------------------
const KECCAK_RC: bigint[] = [
  0x0000000000000001n, 0x0000000000008082n, 0x800000000000808an,
  0x8000000080008000n, 0x000000000000808bn, 0x0000000080000001n,
  0x8000000080008081n, 0x8000000000008009n, 0x000000000000008an,
  0x0000000000000088n, 0x0000000080008009n, 0x000000008000000an,
  0x000000008000808bn, 0x800000000000008bn, 0x8000000000008089n,
  0x8000000000008003n, 0x8000000000008002n, 0x8000000000000080n,
  0x000000000000800an, 0x800000008000000an, 0x8000000080008081n,
  0x8000000000008080n, 0x0000000080000001n, 0x8000000080008008n,
];
const KECCAK_R: number[] = [
  0, 1, 62, 28, 27,
  36, 44, 6, 55, 20,
  3, 10, 43, 25, 39,
  41, 45, 15, 21, 8,
  18, 2, 61, 56, 14,
];
const MASK64 = 0xffffffffffffffffn;

function rotl64(x: bigint, n: number): bigint {
  const k = BigInt(n % 64);
  return ((x << k) | (x >> (64n - k))) & MASK64;
}

function keccakF1600(state: bigint[]): void {
  for (let round = 0; round < 24; round++) {
    // Theta
    const C: bigint[] = new Array(5);
    for (let x = 0; x < 5; x++) {
      C[x] = state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20];
    }
    const D: bigint[] = new Array(5);
    for (let x = 0; x < 5; x++) {
      D[x] = C[(x + 4) % 5] ^ rotl64(C[(x + 1) % 5], 1);
    }
    for (let i = 0; i < 25; i++) {
      state[i] = state[i] ^ D[i % 5];
    }
    // Rho + Pi
    const B: bigint[] = new Array(25).fill(0n);
    for (let x = 0; x < 5; x++) {
      for (let y = 0; y < 5; y++) {
        const idx = x + 5 * y;
        const newX = y;
        const newY = (2 * x + 3 * y) % 5;
        B[newX + 5 * newY] = rotl64(state[idx], KECCAK_R[idx]);
      }
    }
    // Chi
    for (let y = 0; y < 5; y++) {
      for (let x = 0; x < 5; x++) {
        state[x + 5 * y] = B[x + 5 * y] ^ ((~B[((x + 1) % 5) + 5 * y]) & MASK64 & B[((x + 2) % 5) + 5 * y]);
      }
    }
    // Iota
    state[0] = state[0] ^ KECCAK_RC[round];
  }
}

function keccak256(input: Uint8Array): Uint8Array {
  const rate = 136; // bytes (1088-bit rate for 256-bit output)
  const padded = new Uint8Array(Math.ceil((input.length + 1) / rate) * rate);
  padded.set(input);
  padded[input.length] = 0x01; // Ethereum keccak padding (NOT NIST SHA3 0x06)
  padded[padded.length - 1] |= 0x80;

  const state: bigint[] = new Array(25).fill(0n);
  for (let off = 0; off < padded.length; off += rate) {
    for (let i = 0; i < rate / 8; i++) {
      let lane = 0n;
      for (let b = 0; b < 8; b++) {
        lane |= BigInt(padded[off + i * 8 + b]) << BigInt(8 * b);
      }
      state[i] ^= lane;
    }
    keccakF1600(state);
  }

  const out = new Uint8Array(32);
  for (let i = 0; i < 4; i++) {
    const lane = state[i];
    for (let b = 0; b < 8; b++) {
      out[i * 8 + b] = Number((lane >> BigInt(8 * b)) & 0xffn);
    }
  }
  return out;
}

/**
 * Compute the CREATE2 Magic-proxy address for a given EOA — the byte-mirror
 * of the FROZEN Python `magic_proxy_address(eoa)`.
 *
 *   addr = keccak256(0xff || factory || keccak256(eoa) || initCodeHash)[12:]
 *
 * Throws on a malformed EOA (caller is expected to pre-validate, but we
 * fail loudly on garbage rather than return a silently-wrong address).
 * The returned hex is lowercased to match the Python output exactly.
 */
export function magicProxyAddress(eoa: string): string {
  if (!EVM_ADDRESS_RE.test(eoa)) {
    throw new Error(`Not a valid EVM address: ${eoa}`);
  }
  const eoaBytes = hexToBytes(eoa.toLowerCase());
  const factoryBytes = hexToBytes(MAGIC_PROXY_FACTORY.toLowerCase());
  const initHashBytes = hexToBytes(MAGIC_PROXY_INIT_CODE_HASH.toLowerCase());
  const eoaHash = keccak256(eoaBytes);

  const pre = new Uint8Array(1 + factoryBytes.length + eoaHash.length + initHashBytes.length);
  let off = 0;
  pre[off++] = 0xff;
  pre.set(factoryBytes, off); off += factoryBytes.length;
  pre.set(eoaHash, off); off += eoaHash.length;
  pre.set(initHashBytes, off);

  const digest = keccak256(pre);
  return bytesToHex(digest.slice(-20)).toLowerCase();
}
