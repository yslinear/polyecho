"use server";

/**
 * Read-only USDC / position-value probe — Server Action (PRD §5.1).
 *
 * *** PURE PUBLIC READ — NO SECRETS, NO DB WRITE ***
 * This module exists so the operator-authorised key-import form can show the
 * balance of an address it ALREADY HAS (the EOA — which IS the funder for
 * signature_type 0 — or an explicit operator-entered funder for types 1/2/3).
 * It does NOT, and by the recorded web/Python security model MUST NOT, derive
 * type-1 CREATE2 / type-2/3 funders (frozen-constant, CLI-only). Full
 * auto-detect-all-4-types is the CLI `polyecho account detect <EOA>`.
 *
 * It is the TS mirror of the FROZEN Python
 * `polyecho.shared.accounts.fetch_usdc_balance`:
 *   GET `${dataApiBase}/value?user=<lowercased address>`
 *   → defensively parse `[{"user","value"}]` (or a bare dict; keys
 *     value|balance|usdc|cash) → float.
 *
 * Geo-routing: Polymarket is geo-blocked, so the outbound call is routed
 * through the operator proxy when the `polymarket_proxy_url` `settings` row
 * is present (SAME row + Drizzle pattern app/settings/actions.ts writes). The
 * Node server runtime's global `fetch` is undici-backed and honours a
 * `dispatcher`; the `undici` `ProxyAgent` is imported lazily and the proxy
 * leg fails SOFT if it cannot be constructed (best-effort, never fatal).
 *
 * FAIL-SOFT CONTRACT: every error (bad address / network / proxy / parse /
 * ~10s timeout) resolves to `{ ok:false, valueUsd:null, message:<zh-TW> }`
 * and NEVER throws. There is nothing sensitive here, but the no-log
 * discipline is kept regardless (nothing is logged or echoed).
 */

import { eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { settings } from "@/lib/db/schema";
import { coerceStringSetting } from "@/lib/settings-keys";
import { EVM_ADDRESS_RE } from "@/lib/format";
import { t } from "@/lib/i18n";
import { makeProxyDispatcher } from "./proxy-dispatcher";

/** Mirrors Python `settings.polymarket_data_api_url` default (config.py). */
const DEFAULT_DATA_API_BASE = "https://data-api.polymarket.com";

/** Same `settings` key app/settings/actions.ts writes the proxy URL under. */
const PROXY_SETTINGS_KEY = "polymarket_proxy_url";

/** httpx-client-equivalent request timeout (Python uses timeout_s=10.0). */
const TIMEOUT_MS = 10_000;

export interface ProbeBalanceResult {
  /** false on ANY failure (the call is strictly best-effort). */
  ok: boolean;
  /** Parsed USD position value, or null when unknown / failed. */
  valueUsd: number | null;
  /** zh-TW operator message. Never contains anything sensitive. */
  message: string;
}

/** Resolve the data-api base URL the same way other web config is read. */
function resolveDataApiBase(): string {
  const raw = (process.env.POLYECHO_DATA_API_URL ?? "").trim();
  return (raw || DEFAULT_DATA_API_BASE).replace(/\/+$/, "");
}

/**
 * Defensive parse of the data-api `/value` body — byte-for-byte the Python
 * mapping: a list → first element; then a dict whose first present key in
 * (value, balance, usdc, cash) is coerced to a finite number.
 */
function parseValue(data: unknown): number | null {
  let node: unknown = data;
  if (Array.isArray(node)) {
    if (node.length === 0) return null;
    node = node[0];
  }
  if (node && typeof node === "object") {
    const rec = node as Record<string, unknown>;
    for (const key of ["value", "balance", "usdc", "cash"] as const) {
      const v = rec[key];
      if (v !== null && v !== undefined) {
        const n = typeof v === "number" ? v : Number(v);
        return Number.isFinite(n) ? n : null;
      }
    }
  }
  return null;
}

/**
 * Read the operator proxy URL from `settings` (same Drizzle read pattern as
 * the danger-hash SELECT in app/accounts/actions.ts). Returns "" when absent
 * or unreadable — a DB hiccup must not block a best-effort public read.
 */
async function readProxyUrl(): Promise<string> {
  try {
    const [row] = await db
      .select({ value: settings.value })
      .from(settings)
      .where(eq(settings.key, PROXY_SETTINGS_KEY))
      .limit(1);
    return coerceStringSetting(row?.value).trim();
  } catch {
    return "";
  }
}

/**
 * probeBalance — best-effort, fail-soft public read of an address's
 * Polymarket position value. Never throws; never writes; never logs.
 *
 * @param address 0x + 40 hex (validated with the shared EVM_ADDRESS_RE).
 */
export async function probeBalance(
  address: string
): Promise<ProbeBalanceResult> {
  const failed: ProbeBalanceResult = {
    ok: false,
    valueUsd: null,
    message: t("tables.accounts.import.balanceFailed"),
  };

  const addr = (address ?? "").trim();
  if (!EVM_ADDRESS_RE.test(addr)) return failed;
  const lowered = addr.toLowerCase();

  try {
    const base = resolveDataApiBase();
    const url = `${base}/value?user=${encodeURIComponent(lowered)}`;

    const proxyUrl = await readProxyUrl();
    const dispatcher = proxyUrl
      ? await makeProxyDispatcher(proxyUrl)
      : null;

    const init: RequestInit & { dispatcher?: unknown } = {
      method: "GET",
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(TIMEOUT_MS),
      cache: "no-store",
    };
    if (dispatcher) init.dispatcher = dispatcher;

    const resp = await fetch(url, init);
    if (!resp.ok) return failed;

    const body: unknown = await resp.json();
    const valueUsd = parseValue(body);
    if (valueUsd === null) return failed;

    return {
      ok: true,
      valueUsd,
      message: t("tables.accounts.import.balanceLabel"),
    };
  } catch {
    // Network / proxy / parse / timeout — strictly best-effort (PRD §5.1).
    // Nothing sensitive exists here, but nothing is logged regardless.
    return failed;
  }
}
