import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * setPolymarketProxy server action.
 *
 * Verifies the exact settings-row write contract:
 *  - empty value DELETEs the row (direct connection)
 *  - socks5:// is rejected with the precise message, no write
 *  - non-http(s) is rejected, no write
 *  - valid http(s) UPSERTs { key:'polymarket_proxy_url', value:<bare string> }
 *
 * Drizzle (`@/lib/db`) is fully mocked: no DB, no network.
 */

const onConflictDoUpdate = vi.fn().mockResolvedValue(undefined);
const insertValues = vi.fn(() => ({ onConflictDoUpdate }));
const insert = vi.fn(() => ({ values: insertValues }));
const deleteWhere = vi.fn().mockResolvedValue(undefined);
const del = vi.fn(() => ({ where: deleteWhere }));

vi.mock("@/lib/db", () => ({
  db: {
    insert: (...a: unknown[]) => insert(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import { setPolymarketProxy } from "@/app/settings/actions";

describe("setPolymarketProxy", () => {
  beforeEach(() => {
    insert.mockClear();
    insertValues.mockClear();
    onConflictDoUpdate.mockClear().mockResolvedValue(undefined);
    del.mockClear();
    deleteWhere.mockClear().mockResolvedValue(undefined);
  });

  it("clears (deletes) the row when the value is empty", async () => {
    const res = await setPolymarketProxy("");
    expect(res.ok).toBe(true);
    expect(res.value).toBe("");
    expect(del).toHaveBeenCalledTimes(1);
    expect(insert).not.toHaveBeenCalled();
  });

  it("treats whitespace-only as empty and deletes", async () => {
    const res = await setPolymarketProxy("   ");
    expect(res.ok).toBe(true);
    expect(del).toHaveBeenCalledTimes(1);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects socks5:// with the exact message and writes nothing", async () => {
    const res = await setPolymarketProxy("socks5://127.0.0.1:1080");
    expect(res.ok).toBe(false);
    expect(res.message).toBe("SOCKS not supported; use http(s)");
    expect(insert).not.toHaveBeenCalled();
    expect(del).not.toHaveBeenCalled();
  });

  it("rejects a non-http(s) scheme", async () => {
    const res = await setPolymarketProxy("ftp://example.com");
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/http:\/\/ or https:\/\//);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects an unparseable http(s) URL", async () => {
    const res = await setPolymarketProxy("http://");
    expect(res.ok).toBe(false);
    expect(insert).not.toHaveBeenCalled();
  });

  it("upserts a bare string for a valid http URL", async () => {
    const res = await setPolymarketProxy("http://127.0.0.1:8080");
    expect(res.ok).toBe(true);
    expect(insert).toHaveBeenCalledTimes(1);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.key).toBe("polymarket_proxy_url");
    expect(row.value).toBe("http://127.0.0.1:8080");
    expect(typeof row.value).toBe("string"); // bare string, NOT {value:...}
    expect(row.updated_at).toBeInstanceOf(Date);
    // onConflictDoUpdate carries the same bare string.
    const conflict = onConflictDoUpdate.mock.calls[0][0] as {
      set: Record<string, unknown>;
    };
    expect(conflict.set.value).toBe("http://127.0.0.1:8080");
  });

  it("accepts https URLs too", async () => {
    const res = await setPolymarketProxy("https://proxy.local:3128");
    expect(res.ok).toBe(true);
    expect(insert).toHaveBeenCalledTimes(1);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("https://proxy.local:3128");
  });

  it("reads the value from FormData when given a form", async () => {
    const fd = new FormData();
    fd.set("polymarket_proxy_url", "http://10.0.0.1:8888");
    const res = await setPolymarketProxy(fd);
    expect(res.ok).toBe(true);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("http://10.0.0.1:8888");
  });
});
