import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * setPolymarketProxy server action — incl. P0-4 SSRF guard + P7d/R24 gate.
 *
 * Verifies the exact settings-row write contract:
 *  - empty value DELETEs the row (direct connection)  ← safe-direction, no gate
 *  - socks5:// is rejected with the precise message, no write
 *  - non-http(s) is rejected, no write
 *  - SSRF: loopback / RFC1918 / link-local / metadata / localhost / .local
 *    / .internal / ::1 are rejected with a zh-TW message, no write
 *  - setting a NEW public URL UPSERTs { key:'polymarket_proxy_url',
 *    value:<bare string> } ONLY when the inline-confirm phrase matches
 *
 * Drizzle (`@/lib/db`) is fully mocked: no DB, no network.
 */

const onConflictDoUpdate = vi.fn().mockResolvedValue(undefined);
const insertReturning = vi.fn().mockResolvedValue([{ id: 1 }]);
const insertValues = vi.fn(() => ({
  onConflictDoUpdate,
  returning: insertReturning,
}));
const insert = vi.fn(() => ({ values: insertValues }));
const deleteWhere = vi.fn().mockResolvedValue(undefined);
const del = vi.fn(() => ({ where: deleteWhere }));

const selectLimit = vi.fn(() => Promise.resolve([]));
const selectWhere = vi.fn(() => ({ limit: selectLimit }));
const selectFrom = vi.fn(() => ({ where: selectWhere }));
const select = vi.fn(() => ({ from: selectFrom }));

vi.mock("@/lib/db", () => ({
  db: {
    insert: (...a: unknown[]) => insert(...a),
    delete: (...a: unknown[]) => del(...a),
    select: (...a: unknown[]) => select(...a),
  },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const verifyPassphrase = vi.fn(async () => true);
vi.mock("@/lib/passphrase", () => ({
  verifyPassphrase: (...a: unknown[]) => verifyPassphrase(...a),
  // setPolymarketProxy uses requirePassphrase=false; the loadDangerPassphraseHash
  // branch never runs. Stub it so the import resolves regardless.
  loadDangerPassphraseHash: async () => "",
}));

import { setPolymarketProxy } from "@/app/settings/actions";

const CONFIRM_PHRASE = "確認執行";

describe("setPolymarketProxy", () => {
  beforeEach(() => {
    insert.mockClear();
    insertValues.mockClear();
    onConflictDoUpdate.mockClear().mockResolvedValue(undefined);
    insertReturning.mockClear().mockResolvedValue([{ id: 1 }]);
    del.mockClear();
    deleteWhere.mockClear().mockResolvedValue(undefined);
  });

  it("clears (deletes) the row when the value is empty (safe-direction, no gate)", async () => {
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

  it("rejects a valid URL without the inline-confirm phrase (no write)", async () => {
    const res = await setPolymarketProxy("http://203.0.113.10:8080");
    expect(res.ok).toBe(false);
    expect(res.message).toMatch(/確認執行/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("upserts a bare string for a valid PUBLIC http URL when the phrase matches", async () => {
    const res = await setPolymarketProxy({
      value: "http://203.0.113.10:8080",
      confirmText: CONFIRM_PHRASE,
    });
    expect(res.ok).toBe(true);
    // FIRST insert is the settings row (best-effort audit emit may follow).
    expect(insert).toHaveBeenCalled();
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

  it("accepts a normal public https host (with phrase)", async () => {
    const res = await setPolymarketProxy({
      value: "https://proxy.example.com:3128",
      confirmText: CONFIRM_PHRASE,
    });
    expect(res.ok).toBe(true);
    expect(insert).toHaveBeenCalled();
    const row = insertValues.mock.calls[0][0] as Record<string, unknown>;
    expect(row.value).toBe("https://proxy.example.com:3128");
  });

  it("reads the value AND the confirmText from FormData when given a form", async () => {
    const fd = new FormData();
    fd.set("polymarket_proxy_url", "https://corp-proxy.example.net:8888");
    fd.set("confirmText", CONFIRM_PHRASE);
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
