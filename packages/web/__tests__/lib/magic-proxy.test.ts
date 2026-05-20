import { describe, it, expect } from "vitest";
import {
  magicProxyAddress,
  MAGIC_PROXY_FACTORY,
  MAGIC_PROXY_INIT_CODE_HASH,
} from "@/lib/magic-proxy";

/**
 * Parity vector against the FROZEN Python `magic_proxy_address`. The vector
 * was generated with:
 *
 *   uv run --package polyecho-shared python -c \
 *     "from polyecho.shared.polymarket.wallet import magic_proxy_address; \
 *      print(magic_proxy_address('0xAbC0000000000000000000000000000000000123'))"
 *   → 0xfaffc82a2d2c9b2d8a52fd2b66585b58572ec427
 *
 * If THIS test fails, either (a) the TS keccak256 implementation drifted,
 * or (b) the mirrored CREATE2 constants drifted from
 * `packages/shared/src/polyecho/shared/constants.py`. Both are fatal for
 * the P2 CREATE2 cross-check warning — the warn channel would lie.
 */
describe("magic-proxy.ts (TS byte-mirror of Python magic_proxy_address)", () => {
  it("mirrors Python output for the known test vector (0xAbC0…0123)", () => {
    const out = magicProxyAddress("0xAbC0000000000000000000000000000000000123");
    expect(out).toBe("0xfaffc82a2d2c9b2d8a52fd2b66585b58572ec427");
  });

  it("is case-insensitive on the EOA and returns lowercase", () => {
    const lower = magicProxyAddress(
      "0xabc0000000000000000000000000000000000123"
    );
    const upper = magicProxyAddress(
      "0xABC0000000000000000000000000000000000123"
    );
    expect(lower).toBe(upper);
    expect(lower).toBe(lower.toLowerCase());
  });

  it("throws on malformed EOA (caller pre-validates)", () => {
    expect(() => magicProxyAddress("not-an-address")).toThrow();
    expect(() => magicProxyAddress("0x123")).toThrow();
  });

  it("mirrors the Python frozen constants verbatim", () => {
    // Pinning these two strings here catches an accidental edit to the
    // mirror module — if someone changes them, this test fires AND the
    // parity vector test above fires.
    expect(MAGIC_PROXY_FACTORY).toBe(
      "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
    );
    expect(MAGIC_PROXY_INIT_CODE_HASH).toBe(
      "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
    );
  });
});
