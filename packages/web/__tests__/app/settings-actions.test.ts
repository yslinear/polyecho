import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * setPolymarketProxy server action — incl. P0-4 SSRF guard.
 *
 * Verifies the exact settings-row write contract:
 *  - empty value DELETEs the row (direct connection)
 *  - socks5:// is rejected with the precise message, no write
 *  - non-http(s) is rejected, no write
 *  - SSRF: loopback / RFC1918 / link-local / metadata / localhost / .local
 *    / .internal / ::1 are rejected with a zh-TW message, no write
 *  - a normal public https host UPSERTs { key:'polymarket_proxy_url',
 *    value:<bare string> }
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

  it("upserts a bare string for a valid PUBLIC http URL", async () => {
    const res = await setPolymarketProxy("http://203.0.113.10:8080");
    expect(res.ok).toBe(true);
    expect(insert).toHaveBeenCalledTimes(1);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.key).toBe("polymarket_proxy_url");
    expect(row.value).toBe("http://203.0.113.10:8080");
    expect(typeof row.value).toBe("string"); // bare string, NOT {value:...}
    expect(row.updated_at).toBeInstanceOf(Date);
    const conflict = onConflictDoUpdate.mock.calls[0][0] as {
      set: Record<string, unknown>;
    };
    expect(conflict.set.value).toBe("http://203.0.113.10:8080");
  });

  it("accepts a normal public https host", async () => {
    const res = await setPolymarketProxy("https://proxy.example.com:3128");
    expect(res.ok).toBe(true);
    expect(insert).toHaveBeenCalledTimes(1);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("https://proxy.example.com:3128");
  });

  it("reads the value from FormData when given a form", async () => {
    const fd = new FormData();
    fd.set("polymarket_proxy_url", "https://corp-proxy.example.net:8888");
    const res = await setPolymarketProxy(fd);
    expect(res.ok).toBe(true);
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("https://corp-proxy.example.net:8888");
  });

  // --- P0-4 SSRF guard ----------------------------------------------------
  describe("SSRF guard rejects dangerous targets (no write)", () => {
    const blocked = [
      ["http://127.0.0.1:8080", "loopback IPv4"],
      ["http://localhost:8080", "localhost name"],
      ["http://10.0.0.1:8888", "RFC1918 10/8"],
      ["http://172.16.5.4:3128", "RFC1918 172.16/12"],
      ["http://192.168.1.10:3128", "RFC1918 192.168/16"],
      ["http://169.254.1.1:80", "link-local 169.254/16"],
      ["http://169.254.169.254/latest/meta-data/", "cloud metadata IP"],
      ["http://0.0.0.0:8080", "0.0.0.0"],
      ["https://proxy.local:3128", ".local name"],
      ["https://svc.internal:3128", ".internal name"],
      ["http://[::1]:8080", "IPv6 loopback ::1"],
      ["socks5://1.2.3.4:1080", "socks scheme"],
    ] as const;

    for (const [url, label] of blocked) {
      it(`rejects ${label}: ${url}`, async () => {
        const res = await setPolymarketProxy(url);
        expect(res.ok).toBe(false);
        expect(insert).not.toHaveBeenCalled();
        expect(del).not.toHaveBeenCalled();
      });
    }
  });
});
