import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Read-only USDC/value probe Server Action (`app/accounts/balance.ts`).
 *
 * This is a PURE PUBLIC READ — no secrets, no DB write. The tests prove:
 *   - happy path: data-api `[{user,value}]` → number, ok:true
 *   - defensive parsing: bare dict + alternate keys (balance|usdc|cash)
 *   - a non-address input is rejected WITHOUT any fetch / DB call
 *   - network / parse / non-2xx errors fail SOFT to {ok:false,null} (no throw)
 *   - when the `polymarket_proxy_url` settings row is present, the proxy
 *     leg is exercised (undici ProxyAgent dispatcher attached to fetch)
 *
 * Drizzle (`@/lib/db`), `undici`, and global `fetch` are fully mocked: no
 * real network, no real DB. The shared EVM_ADDRESS_RE runs for real.
 */

// --- proxy-row SELECT chain: db.select(..).from(..).where(..).limit(1) ----
const selLimit = vi.fn();
const selWhere = vi.fn(() => ({ limit: selLimit }));
const selFrom = vi.fn(() => ({ where: selWhere }));
const select = vi.fn(() => ({ from: selFrom }));

vi.mock("@/lib/db", () => ({
  db: { select: (...a: unknown[]) => select(...a) },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));
vi.mock("@/lib/db/schema", () => ({
  settings: { key: "settings.key", value: "settings.value" },
}));
vi.mock("@/lib/settings-keys", () => ({
  coerceStringSetting: (v: unknown) =>
    typeof v === "string"
      ? v
      : v && typeof v === "object" && "value" in v
        ? String((v as { value: unknown }).value)
        : "",
}));

// The undici-isolation seam is mocked by its LOCAL path so the test
// runner never has to resolve the bare `undici` specifier. We assert the
// proxy leg by spying on makeProxyDispatcher + returning a sentinel.
const ProxyAgentCtor = vi.fn();
class FakeProxyAgent {
  constructor(url: string) {
    ProxyAgentCtor(url);
  }
}
const makeProxyDispatcher = vi.fn(async (proxyUrl: string) =>
  /^https?:\/\//i.test(proxyUrl) ? new FakeProxyAgent(proxyUrl) : null
);
vi.mock("@/app/accounts/proxy-dispatcher", () => ({
  makeProxyDispatcher: (...a: unknown[]) => makeProxyDispatcher(...(a as [string])),
}));

import {
  probeBalance,
  probeIdleUsdc,
  probeTotalBalance,
} from "@/app/accounts/balance";

const ADDR = "0x" + "ab".repeat(20); // valid 0x+40hex
const fetchMock = vi.fn();

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    status: ok ? 200 : 502,
    json: async () => body,
  } as unknown as Response;
}

/** JSON-RPC response shape used by the eth_call idle-USDC probe. */
function rpcResponse(
  result: unknown,
  ok = true,
  extra: Record<string, unknown> = {}
): Response {
  return {
    ok,
    status: ok ? 200 : 502,
    json: async () => ({ jsonrpc: "2.0", id: 1, result, ...extra }),
  } as unknown as Response;
}

describe("probeBalance (read-only data-api /value mirror)", () => {
  beforeEach(() => {
    select.mockClear();
    selFrom.mockClear();
    selWhere.mockClear();
    selLimit.mockClear().mockResolvedValue([]); // no proxy row by default
    ProxyAgentCtor.mockClear();
    makeProxyDispatcher.mockClear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    delete process.env.POLYECHO_DATA_API_URL;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("valid address happy path: [{user,value}] → number, ok:true", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ user: ADDR, value: 1234.56 }])
    );
    const res = await probeBalance(ADDR);
    expect(res.ok).toBe(true);
    expect(res.valueUsd).toBe(1234.56);
    expect(typeof res.message).toBe("string");

    // It hit the data-api /value endpoint with the lowercased address.
    const calledUrl = String(fetchMock.mock.calls[0][0]);
    expect(calledUrl).toContain("https://data-api.polymarket.com/value");
    expect(calledUrl).toContain(`user=${ADDR.toLowerCase()}`);
  });

  it("defensively parses a bare dict and the alternate keys", async () => {
    // bare dict, key "value"
    fetchMock.mockResolvedValueOnce(jsonResponse({ value: 10 }));
    expect((await probeBalance(ADDR)).valueUsd).toBe(10);

    // alternate key "balance"
    fetchMock.mockResolvedValueOnce(jsonResponse({ balance: "42.5" }));
    expect((await probeBalance(ADDR)).valueUsd).toBe(42.5);

    // alternate key "usdc"
    fetchMock.mockResolvedValueOnce(jsonResponse([{ usdc: 7 }]));
    expect((await probeBalance(ADDR)).valueUsd).toBe(7);

    // alternate key "cash"
    fetchMock.mockResolvedValueOnce(jsonResponse({ cash: 0 }));
    const zero = await probeBalance(ADDR);
    expect(zero.ok).toBe(true);
    expect(zero.valueUsd).toBe(0);
  });

  it("rejects a non-address WITHOUT any fetch or DB call (fail-soft)", async () => {
    const res = await probeBalance("0xnothex");
    expect(res.ok).toBe(false);
    expect(res.valueUsd).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(select).not.toHaveBeenCalled();
  });

  it("a network error fails soft to {ok:false, null} and never throws", async () => {
    fetchMock.mockRejectedValueOnce(new Error("ENOTFOUND data-api"));
    const res = await probeBalance(ADDR);
    expect(res.ok).toBe(false);
    expect(res.valueUsd).toBeNull();
    expect(res.message).not.toContain("ENOTFOUND");
  });

  it("an unparseable / empty body fails soft (null, no throw)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([])); // empty list
    expect((await probeBalance(ADDR)).valueUsd).toBeNull();

    fetchMock.mockResolvedValueOnce(jsonResponse({ unrelated: 1 }));
    expect((await probeBalance(ADDR)).ok).toBe(false);

    fetchMock.mockResolvedValueOnce(jsonResponse({}, false)); // 502
    expect((await probeBalance(ADDR)).ok).toBe(false);
  });

  it("routes through the operator proxy when the settings row is present", async () => {
    selLimit.mockResolvedValueOnce([{ value: "http://10.0.0.9:8888" }]);
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ user: ADDR, value: 99 }])
    );

    const res = await probeBalance(ADDR);

    expect(res.ok).toBe(true);
    expect(res.valueUsd).toBe(99);
    // The proxy URL was read from settings and passed to the dispatcher
    // builder, which produced an undici ProxyAgent...
    expect(select).toHaveBeenCalledTimes(1);
    expect(makeProxyDispatcher).toHaveBeenCalledWith("http://10.0.0.9:8888");
    expect(ProxyAgentCtor).toHaveBeenCalledWith("http://10.0.0.9:8888");
    // ...attached to the fetch init as `dispatcher`.
    const init = fetchMock.mock.calls[0][1] as { dispatcher?: unknown };
    expect(init.dispatcher).toBeInstanceOf(FakeProxyAgent);
  });

  it("a non-http(s) proxy value is ignored (no dispatcher, still probes)", async () => {
    selLimit.mockResolvedValueOnce([{ value: "socks5://10.0.0.9:1080" }]);
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ user: ADDR, value: 5 }])
    );

    const res = await probeBalance(ADDR);
    expect(res.ok).toBe(true);
    expect(ProxyAgentCtor).not.toHaveBeenCalled();
    const init = fetchMock.mock.calls[0][1] as { dispatcher?: unknown };
    expect(init.dispatcher).toBeUndefined();
  });

  it("a DB error reading the proxy row does NOT block the probe", async () => {
    selLimit.mockRejectedValueOnce(new Error("pg down"));
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ user: ADDR, value: 3 }])
    );
    const res = await probeBalance(ADDR);
    expect(res.ok).toBe(true);
    expect(res.valueUsd).toBe(3);
    expect(ProxyAgentCtor).not.toHaveBeenCalled();
  });

  it("honours POLYECHO_DATA_API_URL override (trailing slash trimmed)", async () => {
    process.env.POLYECHO_DATA_API_URL = "https://proxy.example.com/api/";
    fetchMock.mockResolvedValueOnce(jsonResponse([{ value: 1 }]));
    await probeBalance(ADDR);
    const calledUrl = String(fetchMock.mock.calls[0][0]);
    expect(calledUrl).toBe(
      `https://proxy.example.com/api/value?user=${ADDR.toLowerCase()}`
    );
  });
});

/**
 * probeIdleUsdc — on-chain Polygon JSON-RPC `eth_call` to USDC.e
 * `balanceOf(address)`. The settings table can supply `polygon_rpc_url`;
 * otherwise env `POLYECHO_POLYGON_RPC_URL`; otherwise the dRPC default.
 * The proxy row is NEVER consulted (Polygon dRPC is not geo-blocked).
 */
describe("probeIdleUsdc (on-chain USDC.e balanceOf, direct RPC)", () => {
  beforeEach(() => {
    select.mockClear();
    selFrom.mockClear();
    selWhere.mockClear();
    selLimit.mockClear().mockResolvedValue([]); // no settings row by default
    ProxyAgentCtor.mockClear();
    makeProxyDispatcher.mockClear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    delete process.env.POLYECHO_POLYGON_RPC_URL;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** USDC.e balanceOf(address) calldata: selector + 32-byte padded addr. */
  function expectedCallData(addr: string): string {
    return "0x70a08231" + addr.toLowerCase().slice(2).padStart(64, "0");
  }

  it("happy path: result 0x...f4240 (1e6) → 1 USDC, ok:true", async () => {
    fetchMock.mockResolvedValueOnce(rpcResponse("0x00000000000000000000000000000000000000000000000000000000000f4240"));
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(true);
    expect(res.idleUsdc).toBe(1);

    // Default dRPC endpoint when no settings/env override.
    expect(fetchMock.mock.calls[0][0]).toBe("https://polygon.drpc.org");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({
      jsonrpc: "2.0",
      method: "eth_call",
      id: 1,
    });
    expect(body.params).toEqual([
      {
        to: "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        data: expectedCallData(ADDR),
      },
      "latest",
    ]);
  });

  it("fractional value 1.234567 USDC preserves sub-cent precision", async () => {
    // 1.234567 USDC = 1_234_567 raw → 0x12d687
    fetchMock.mockResolvedValueOnce(rpcResponse("0x000000000000000000000000000000000000000000000000000000000012d687"));
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(true);
    expect(res.idleUsdc).toBeCloseTo(1.234567, 6);
  });

  it("1M-scale value (1_000_000 USDC) preserves whole-number scale", async () => {
    // 1_000_000 USDC = 1e12 raw → 0xe8d4a51000
    fetchMock.mockResolvedValueOnce(rpcResponse("0x000000000000000000000000000000000000000000000000000000e8d4a51000"));
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(true);
    expect(res.idleUsdc).toBe(1_000_000);
  });

  it("\"0x\" empty result → 0 USDC, ok:true", async () => {
    fetchMock.mockResolvedValueOnce(rpcResponse("0x"));
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(true);
    expect(res.idleUsdc).toBe(0);
  });

  it("rejects a non-address WITHOUT any fetch or DB call", async () => {
    const res = await probeIdleUsdc("0xnothex");
    expect(res.ok).toBe(false);
    expect(res.idleUsdc).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(select).not.toHaveBeenCalled();
  });

  it("RPC non-2xx fails soft to {ok:false, null}", async () => {
    fetchMock.mockResolvedValueOnce(rpcResponse("0x0", false));
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(false);
    expect(res.idleUsdc).toBeNull();
  });

  it("malformed hex result fails soft (not 0x-prefixed hex)", async () => {
    fetchMock.mockResolvedValueOnce(rpcResponse("not-a-hex-string"));
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(false);
    expect(res.idleUsdc).toBeNull();
  });

  it("missing `result` field fails soft", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ jsonrpc: "2.0", id: 1, error: { code: -32603 } }),
    } as unknown as Response);
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(false);
    expect(res.idleUsdc).toBeNull();
  });

  it("abort/timeout (rejected fetch) fails soft and never throws", async () => {
    fetchMock.mockRejectedValueOnce(new Error("AbortError: timeout"));
    const res = await probeIdleUsdc(ADDR);
    expect(res.ok).toBe(false);
    expect(res.idleUsdc).toBeNull();
    expect(res.message).not.toContain("AbortError");
  });

  it("uses the dRPC default when no settings row and no env var", async () => {
    fetchMock.mockResolvedValueOnce(rpcResponse("0x0"));
    await probeIdleUsdc(ADDR);
    expect(fetchMock.mock.calls[0][0]).toBe("https://polygon.drpc.org");
  });

  it("settings-row override is taken over env/default", async () => {
    selLimit.mockResolvedValueOnce([{ value: "https://rpc.example.com" }]);
    process.env.POLYECHO_POLYGON_RPC_URL = "https://env.example.com";
    fetchMock.mockResolvedValueOnce(rpcResponse("0x0"));
    await probeIdleUsdc(ADDR);
    expect(fetchMock.mock.calls[0][0]).toBe("https://rpc.example.com");
  });

  it("env-var fallback is used when settings row is absent", async () => {
    process.env.POLYECHO_POLYGON_RPC_URL = "https://env.example.com";
    fetchMock.mockResolvedValueOnce(rpcResponse("0x0"));
    await probeIdleUsdc(ADDR);
    expect(fetchMock.mock.calls[0][0]).toBe("https://env.example.com");
  });

  it("does NOT route the dRPC call through the polymarket proxy", async () => {
    // Even if a polymarket proxy is configured, the on-chain leg is direct.
    fetchMock.mockResolvedValueOnce(rpcResponse("0x0"));
    await probeIdleUsdc(ADDR);
    expect(ProxyAgentCtor).not.toHaveBeenCalled();
    const init = fetchMock.mock.calls[0][1] as { dispatcher?: unknown };
    expect(init.dispatcher).toBeUndefined();
  });
});

/**
 * probeTotalBalance — runs probeBalance + probeIdleUsdc in parallel and
 * composes a total. A successful half is NEVER zeroed by the other
 * half's failure. `ok` is true iff at least one sub-probe succeeded.
 */
describe("probeTotalBalance (positions + idle USDC composition)", () => {
  beforeEach(() => {
    select.mockClear();
    selFrom.mockClear();
    selWhere.mockClear();
    selLimit.mockClear().mockResolvedValue([]);
    ProxyAgentCtor.mockClear();
    makeProxyDispatcher.mockClear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    delete process.env.POLYECHO_DATA_API_URL;
    delete process.env.POLYECHO_POLYGON_RPC_URL;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** Steer fetches by URL so positions + idle resolve independently. */
  function routedFetch(opts: {
    positions?: { body: unknown; ok?: boolean } | "reject";
    idle?: { result?: unknown; ok?: boolean } | "reject";
  }) {
    fetchMock.mockImplementation(async (url: unknown) => {
      const u = String(url);
      if (u.includes("/value")) {
        if (opts.positions === "reject")
          throw new Error("ENOTFOUND data-api");
        if (!opts.positions) throw new Error("unexpected positions call");
        return jsonResponse(opts.positions.body, opts.positions.ok ?? true);
      }
      // anything else: assume the RPC leg
      if (opts.idle === "reject") throw new Error("ENOTFOUND rpc");
      if (!opts.idle) throw new Error("unexpected idle call");
      return rpcResponse(opts.idle.result ?? "0x0", opts.idle.ok ?? true);
    });
  }

  it("both ok: totals sum positionsUsd + idleUsdc", async () => {
    routedFetch({
      positions: { body: [{ value: 50 }] },
      // 12.5 USDC = 12_500_000 raw → 0xbebc20
      idle: { result: "0x0000000000000000000000000000000000000000000000000000000000bebc20" },
    });
    const res = await probeTotalBalance(ADDR);
    expect(res.ok).toBe(true);
    expect(res.positionsUsd).toBe(50);
    expect(res.idleUsdc).toBe(12.5);
    expect(res.totalUsd).toBe(62.5);
  });

  it("positions-only ok: idle null, total equals positionsUsd", async () => {
    routedFetch({
      positions: { body: [{ value: 75 }] },
      idle: "reject",
    });
    const res = await probeTotalBalance(ADDR);
    expect(res.ok).toBe(true);
    expect(res.positionsUsd).toBe(75);
    expect(res.idleUsdc).toBeNull();
    expect(res.totalUsd).toBe(75);
  });

  it("idle-only ok: positions null, total equals idleUsdc (NEVER zeroed)", async () => {
    routedFetch({
      positions: "reject",
      // 7 USDC = 7_000_000 raw → 0x6acfc0
      idle: { result: "0x00000000000000000000000000000000000000000000000000000000006acfc0" },
    });
    const res = await probeTotalBalance(ADDR);
    expect(res.ok).toBe(true);
    expect(res.positionsUsd).toBeNull();
    expect(res.idleUsdc).toBe(7);
    expect(res.totalUsd).toBe(7);
  });

  it("both fail: total null, ok false", async () => {
    routedFetch({ positions: "reject", idle: "reject" });
    const res = await probeTotalBalance(ADDR);
    expect(res.ok).toBe(false);
    expect(res.positionsUsd).toBeNull();
    expect(res.idleUsdc).toBeNull();
    expect(res.totalUsd).toBeNull();
  });

  it("rejects a non-address WITHOUT any fetch or DB call", async () => {
    const res = await probeTotalBalance("not-an-address");
    expect(res.ok).toBe(false);
    expect(res.totalUsd).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
