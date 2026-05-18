"use server";

/**
 * Settings Server Actions — PRD §6.1 / §8.1 "安全寫入" tier.
 *
 * Completes the Polymarket-proxy backend feature: trader-core reads the
 * `settings` row keyed `polymarket_proxy_url`. Its `_coerce_proxy` accepts
 * EITHER a bare JSON string OR `{"value": "..."}`. We store the SIMPLEST
 * shape — a bare string in the jsonb `value` column.
 *
 * Empty value  -> the row is deleted (treated as "direct, no proxy").
 * Non-empty     -> must be http:// or https://. socks5:// is rejected with a
 *                  clear message (trader-core's httpx transport is http(s)-only).
 *
 * Drizzle import/style mirrors app/backtest/actions.ts.
 */

import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { settings } from "@/lib/db/schema";
import {
  RISK_MODE_KEY,
  GLOBAL_PAUSED_KEY,
  type RiskMode,
} from "@/lib/settings-keys";

const SETTINGS_KEY = "polymarket_proxy_url";

/**
 * P0-4 (web half) — SSRF guard for the Polymarket proxy URL.
 *
 * Validate BEFORE persisting. trader-core dials whatever we store, so a
 * loopback / RFC1918 / link-local / cloud-metadata target would let the
 * dashboard pivot into internal services. We block:
 *   - any scheme other than http/https (socks*, file:, gopher:, etc.)
 *   - empty host
 *   - literal IPs in loopback / RFC1918 / link-local / unique-local ranges
 *   - the cloud metadata IP 169.254.169.254, 0.0.0.0, ::1, ::
 *   - the obvious localhost-ish names: localhost, *.local, *.internal
 *   - parse failures
 *
 * Residual risk (documented): a hostname that is NOT a literal IP and not
 * one of the blocked names could still DNS-resolve to a private IP
 * (DNS-rebind). We do not resolve DNS here; that residual is accepted and
 * called out — full mitigation belongs to the dialing side (trader-core).
 */
function ipv4ToParts(host: string): number[] | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return null;
  const parts = m.slice(1, 5).map((s) => Number.parseInt(s, 10));
  if (parts.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return null;
  return parts;
}

function isBlockedIpv4(parts: number[]): boolean {
  const [a, b] = parts;
  if (a === 127) return true; // 127.0.0.0/8 loopback
  if (a === 10) return true; // 10.0.0.0/8 RFC1918
  if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12 RFC1918
  if (a === 192 && b === 168) return true; // 192.168.0.0/16 RFC1918
  if (a === 169 && b === 254) return true; // 169.254.0.0/16 link-local (+metadata)
  if (a === 0) return true; // 0.0.0.0/8 "this host"
  return false;
}

/** Return a zh-TW rejection reason if the host is SSRF-dangerous, else null. */
function ssrfReject(rawHostname: string): string | null {
  // URL.hostname strips brackets from IPv6 but keeps the literal text.
  const host = rawHostname.trim().toLowerCase();
  if (!host) return "代理位址缺少主機名稱";

  // Obvious localhost-ish / internal names.
  if (host === "localhost") return "不可使用 localhost 作為代理主機";
  if (host.endsWith(".local") || host.endsWith(".internal")) {
    return "不可使用 .local / .internal 內網名稱作為代理主機";
  }

  // IPv6 literals (URL.hostname has already removed the [] brackets).
  if (host.includes(":")) {
    const v6 = host.replace(/^\[|\]$/g, "");
    if (
      v6 === "::1" || // loopback
      v6 === "::" || // unspecified
      v6 === "0:0:0:0:0:0:0:1" ||
      v6 === "0:0:0:0:0:0:0:0" ||
      v6.startsWith("fe80:") || // link-local fe80::/10
      v6.startsWith("fc") || // unique-local fc00::/7
      v6.startsWith("fd") ||
      v6.startsWith("::ffff:127.") || // IPv4-mapped loopback
      v6.startsWith("::ffff:10.") ||
      v6.startsWith("::ffff:192.168.") ||
      v6.startsWith("::ffff:169.254.")
    ) {
      return "不可使用本機/內網 IPv6 位址作為代理主機";
    }
    return null; // some other global IPv6 — allowed
  }

  // IPv4 literal.
  const v4 = ipv4ToParts(host);
  if (v4) {
    if (v4[0] === 169 && v4[1] === 254 && v4[2] === 169 && v4[3] === 254) {
      return "不可使用雲端中繼資料位址 169.254.169.254 作為代理主機";
    }
    if (isBlockedIpv4(v4)) {
      return "不可使用回送/內網/連結本地 IP 作為代理主機";
    }
  }

  return null;
}

export interface SetProxyResult {
  ok: boolean;
  /** Human message for the toast (success or validation error). */
  message: string;
  /** The value that is now in effect ("" === direct). */
  value: string;
}

/**
 * setPolymarketProxy — called from the /settings client form.
 *
 * Accepts a FormData (native <form action>) or a bare string so the call
 * site can stay simple. Validates, then upserts (or deletes when cleared).
 */
export async function setPolymarketProxy(
  input: FormData | string
): Promise<SetProxyResult> {
  const raw =
    typeof input === "string"
      ? input
      : String(input.get("polymarket_proxy_url") ?? "");

  const trimmed = raw.trim();

  // --- Empty -> clear the row (direct connection) ---------------------------
  if (trimmed === "") {
    await db.delete(settings).where(eq(settings.key, SETTINGS_KEY));
    return {
      ok: true,
      message: "Proxy cleared. Polymarket calls now go direct.",
      value: "",
    };
  }

  // --- Reject SOCKS explicitly (httpx transport is http(s)-only) -----------
  if (/^socks\d?:\/\//i.test(trimmed)) {
    return {
      ok: false,
      message: "SOCKS not supported; use http(s)",
      value: trimmed,
    };
  }

  // --- Must be http:// or https:// -----------------------------------------
  if (!/^https?:\/\//i.test(trimmed)) {
    return {
      ok: false,
      message: "Proxy URL must start with http:// or https://",
      value: trimmed,
    };
  }

  // Reject anything that is not a parseable URL (e.g. "http://"), and
  // SSRF-validate the host BEFORE persisting (trader-core dials it).
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
    if (!parsed.hostname) throw new Error("no host");
    // Defence in depth: URL parsing already normalised the scheme, but a
    // crafted authority could still slip a non-http scheme past the regex.
    const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();
    if (scheme !== "http" && scheme !== "https") {
      throw new Error("non-http(s) scheme");
    }
  } catch {
    return {
      ok: false,
      message: "Enter a valid http(s) URL, e.g. http://203.0.113.10:8080",
      value: trimmed,
    };
  }

  const ssrf = ssrfReject(parsed.hostname);
  if (ssrf) {
    return {
      ok: false,
      message: `${ssrf}。請改用對外可路由的 http(s) 代理位址。`,
      value: trimmed,
    };
  }

  // --- Upsert the bare string into the jsonb value column -------------------
  const updated_at = new Date();
  await db
    .insert(settings)
    .values({ key: SETTINGS_KEY, value: trimmed, updated_at })
    .onConflictDoUpdate({
      target: settings.key,
      set: { value: trimmed, updated_at },
    });

  return {
    ok: true,
    message: "Proxy saved. Takes effect on the next Polymarket call.",
    value: trimmed,
  };
}

// --- 旅行/保守模式 (PRD §5.6) --------------------------------------------

export interface SetRiskModeResult {
  ok: boolean;
  message: string;
  value: RiskMode;
}

/**
 * setRiskMode — upsert the `risk_mode` settings row as a BARE STRING
 * ("normal" | "conservative"), IDENTICAL shape convention to the
 * polymarket_proxy_url row above. Python `polyecho.shared.risk_mode` reads
 * this live; conservative tightens copy ratio + risk caps (PRD §5.6).
 */
export async function setRiskMode(
  mode: RiskMode
): Promise<SetRiskModeResult> {
  const value: RiskMode = mode === "conservative" ? "conservative" : "normal";
  const updated_at = new Date();
  await db
    .insert(settings)
    .values({ key: RISK_MODE_KEY, value, updated_at })
    .onConflictDoUpdate({
      target: settings.key,
      set: { value, updated_at },
    });
  return {
    ok: true,
    message:
      value === "conservative"
        ? "已切換為保守/旅行模式：跟單比例與風險上限自動收緊。"
        : "已切換為正常模式。",
    value,
  };
}

// --- 全域暫停/恢復 (PRD §5.8) --------------------------------------------

export interface SetGlobalPausedResult {
  ok: boolean;
  message: string;
  paused: boolean;
}

/**
 * setGlobalPaused — upsert the `global_paused` settings row as the bare
 * string "true" | "false" (same convention). trader-core's reconcile loop
 * honours this as a kill-switch across every follow relation; consistent
 * with the per-leader pause in app/leaders/actions.ts.
 */
export async function setGlobalPaused(
  paused: boolean
): Promise<SetGlobalPausedResult> {
  const value = paused ? "true" : "false";
  const updated_at = new Date();
  await db
    .insert(settings)
    .values({ key: GLOBAL_PAUSED_KEY, value, updated_at })
    .onConflictDoUpdate({
      target: settings.key,
      set: { value, updated_at },
    });
  return {
    ok: true,
    message: paused
      ? "已全域暫停：trader-core 停止所有跟單。"
      : "已恢復：trader-core 於下一筆領隊交易繼續跟單。",
    paused,
  };
}
