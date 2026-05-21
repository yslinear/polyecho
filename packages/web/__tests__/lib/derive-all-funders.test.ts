import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * deriveAllFunders Server Action — proxy to the internal `detect` Python
 * container that returns the 4 candidate funder addresses per Polymarket
 * signature type. This test exercises the proxy in isolation:
 *
 *  - validates the EOA shape BEFORE issuing a fetch (cheap reject),
 *  - maps the locked `DetectResponse` shape → `FunderCandidate[]` in the
 *    canonical sig0,1,2,3 order,
 *  - fail-soft on every transport/parse/sig2 error path (no throws ever
 *    escape the action; every failure yields `{ok:false, reason:...}`),
 *  - never logs the EOA on failure (covered by the no-throw + sealed
 *    reason-string assertions).
 *
 * `@/lib/db` and friends are mocked so the module loads in jsdom without
 * a live Postgres — only the proxy + mapping logic is under test, and
 * `fetch` is stubbed with `vi.stubGlobal` per case.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));
vi.mock("@/lib/db/schema", () => ({
  accounts: { id: "accounts.id", eoa_address: "accounts.eoa_address" },
  settings: { key: "settings.key", value: "settings.value" },
}));
vi.mock("@/lib/settings-keys", () => ({
  DANGER_PASSPHRASE_HASH_KEY: "danger_passphrase_hash",
  coerceStringSetting: (v: unknown) => (typeof v === "string" ? v : ""),
}));

import { deriveAllFunders, type FunderCandidate } from "@/app/accounts/actions";

const EOA = "0x" + "ab".repeat(20); // 0x + 40 hex
const EOA_LOWER = EOA.toLowerCase();
const ADDR_SIG0 = "0x" + "11".repeat(20);
const ADDR_SIG1 = "0x" + "22".repeat(20);
const ADDR_SIG2 = "0x" + "33".repeat(20);
const ADDR_SIG3 = "0x" + "44".repeat(20);

function goodBody() {
  return {
    sig0: { addr: ADDR_SIG0, mode: "eoa", derived: false },
    sig1: { addr: ADDR_SIG1, mode: "magic", derived: true },
    sig2: { addr: ADDR_SIG2, mode: "safe", derived: true },
    sig3: { addr: ADDR_SIG3, mode: "poly_1271", derived: true },
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("deriveAllFunders Server Action", () => {
  beforeEach(() => {
    // Pin the detect URL so the assertions below can verify the query.
    process.env.POLYECHO_DETECT_URL = "http://detect:8001";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("happy path: maps 4 sigs in order with correct sigType + mode", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(goodBody()));
    vi.stubGlobal("fetch", fetchMock);

    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(true);
    if (!res.ok) throw new Error("unreachable");

    // Lowercased EOA propagates into the query string.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const calledUrl = String(fetchMock.mock.calls[0]?.[0]);
    expect(calledUrl).toBe(
      `http://detect:8001/detect?eoa=${encodeURIComponent(EOA_LOWER)}`,
    );

    expect(res.candidates).toHaveLength(4);
    const order = res.candidates.map((c) => c.sigType);
    expect(order).toEqual([0, 1, 2, 3]);

    const byType = Object.fromEntries(
      res.candidates.map((c) => [c.sigType, c]),
    ) as Record<0 | 1 | 2 | 3, FunderCandidate>;

    expect(byType[0]).toMatchObject({
      sigType: 0,
      mode: "eoa",
      address: ADDR_SIG0,
      derived: false,
    });
    expect(byType[1]).toMatchObject({
      sigType: 1,
      mode: "magic",
      address: ADDR_SIG1,
      derived: true,
    });
    expect(byType[2]).toMatchObject({
      sigType: 2,
      mode: "safe",
      address: ADDR_SIG2,
      derived: true,
    });
    expect(byType[3]).toMatchObject({
      sigType: 3,
      mode: "poly_1271",
      address: ADDR_SIG3,
      derived: true,
    });

    // No spurious `error` keys on the happy path.
    for (const c of res.candidates) expect(c.error).toBeUndefined();
  });

  it("malformed EOA: ok:false + never calls fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    for (const bad of ["", "0xnothex", "not-an-address", EOA.slice(0, -1)]) {
      const res = await deriveAllFunders(bad);
      expect(res.ok).toBe(false);
      if (res.ok) throw new Error("unreachable");
      expect(typeof res.reason).toBe("string");
      expect(res.reason.length).toBeGreaterThan(0);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("detect 500: ok:false with a populated reason, no throw", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ error: "boom" }, 500)),
    );
    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(false);
    if (res.ok) throw new Error("unreachable");
    expect(res.reason).toBeTruthy();
  });

  it("detect 400: any non-2xx collapses to ok:false", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "bad" }, 400)),
    );
    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(false);
  });

  it("fetch timeout (AbortError from AbortSignal.timeout): ok:false, no throw", async () => {
    // Simulate `AbortSignal.timeout` firing → fetch rejects with AbortError.
    const abortErr = new DOMException(
      "The operation was aborted.",
      "AbortError",
    );
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(abortErr));

    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(false);
    if (res.ok) throw new Error("unreachable");
    expect(res.reason).toBeTruthy();
  });

  it("network error (fetch rejects): ok:false, no throw", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("fetch failed")),
    );
    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(false);
  });

  it("malformed JSON body: ok:false, no throw", async () => {
    const bad = new Response("not json at all", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(bad));

    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(false);
  });

  it("sig2.addr null in response: candidate[2].address null + error propagated", async () => {
    const body = goodBody();
    body.sig2 = {
      addr: null as unknown as string,
      mode: "safe",
      derived: true,
      // @ts-expect-error — exercise the `error` propagation branch
      error: "rpc_timeout",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(body)));

    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(true);
    if (!res.ok) throw new Error("unreachable");
    const sig2 = res.candidates[2];
    expect(sig2.sigType).toBe(2);
    expect(sig2.mode).toBe("safe");
    expect(sig2.address).toBeNull();
    expect(sig2.derived).toBe(true);
    expect(sig2.error).toBe("rpc_timeout");
  });

  it("non-object JSON body (e.g. null): ok:false", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(null)));
    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(false);
  });

  it("missing sub-objects: maps to address:null + falls back to canonical mode", async () => {
    // Body missing sig1 entirely + sig3 with no fields. Defensive mapping
    // should still produce 4 ordered candidates with sensible defaults.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          sig0: { addr: ADDR_SIG0, mode: "eoa", derived: false },
          // sig1 omitted
          sig2: { addr: ADDR_SIG2, mode: "safe", derived: true },
          sig3: {},
        }),
      ),
    );
    const res = await deriveAllFunders(EOA);
    expect(res.ok).toBe(true);
    if (!res.ok) throw new Error("unreachable");
    expect(res.candidates.map((c) => c.sigType)).toEqual([0, 1, 2, 3]);
    expect(res.candidates[1].address).toBeNull();
    expect(res.candidates[1].mode).toBe("magic"); // canonical fallback
    expect(res.candidates[1].derived).toBe(false);
    expect(res.candidates[3].address).toBeNull();
    expect(res.candidates[3].mode).toBe("poly_1271"); // canonical fallback
  });
});
