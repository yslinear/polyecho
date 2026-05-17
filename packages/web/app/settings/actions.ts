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

  // Reject anything that is not a parseable URL (e.g. "http://").
  try {
    const u = new URL(trimmed);
    if (!u.hostname) throw new Error("no host");
  } catch {
    return {
      ok: false,
      message: "Enter a valid http(s) URL, e.g. http://127.0.0.1:8080",
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
