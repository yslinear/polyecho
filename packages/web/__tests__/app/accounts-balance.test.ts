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

import { probeBalance } from "@/app/accounts/balance";

const ADDR = "0x" + "ab".repeat(20); // valid 0x+40hex
const fetchMock = vi.fn();

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    status: ok ? 200 : 502,
    json: async () => body,
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
