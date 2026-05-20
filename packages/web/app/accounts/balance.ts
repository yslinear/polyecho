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

/**
 * Polygon JSON-RPC URL used for the on-chain idle-USDC.e probe. The
 * settings table can override it via `polygon_rpc_url`; otherwise the
 * `POLYECHO_POLYGON_RPC_URL` env var; otherwise the project's known-good
 * dRPC default (see polyecho-ops-runbook: polygon-rpc.com is dead 401,
 * polygon.drpc.org is the project's documented working endpoint).
 *
 * Polygon dRPC is NOT geo-blocked, so this leg is DIRECT — it must NOT
 * route through `polymarket_proxy_url`.
 */
const POLYGON_RPC_SETTINGS_KEY = "polygon_rpc_url";
const DEFAULT_POLYGON_RPC_URL = "https://polygon.drpc.org";

/**
 * USDC.e (Polygon — bridged 6-decimal USDC, what Polymarket actually
 * settles in). `balanceOf(address)` selector is `0x70a08231`.
 */
const USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
const BALANCE_OF_SELECTOR = "0x70a08231";

/** USDC.e is 6-decimal; divide on-chain integer units by 10^6 to get USD. */
const USDC_DECIMALS = 6n;

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

/** Result of the on-chain idle-USDC.e probe (b). */
export interface ProbeIdleUsdcResult {
  /** false on ANY failure (the call is strictly best-effort). */
  ok: boolean;
  /** USDC.e idle balance (USD; 6-decimal converted), or null on failure. */
  idleUsdc: number | null;
  /** zh-TW operator message. Never contains anything sensitive. */
  message: string;
}

/**
 * Composite balance result combining the data-api positions value (a) and
 * the on-chain idle USDC.e (b). `totalUsd` is the sum, with a non-null
 * half NEVER being zeroed by the other half's failure.
 */
export interface ProbeTotalBalanceResult {
  /** true iff AT LEAST one of the two sub-probes succeeded. */
  ok: boolean;
  /** Polymarket positions value (a), null on failure. */
  positionsUsd: number | null;
  /** On-chain idle USDC.e (b), null on failure. */
  idleUsdc: number | null;
  /** Sum of the two halves (treating null as 0), or null if BOTH failed. */
  totalUsd: number | null;
  /** zh-TW operator message — distinguishes full vs partial failure. */
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

/**
 * Resolve the Polygon JSON-RPC URL using the same precedence the rest of
 * this module follows: settings row → env var → known-good default.
 * Never throws — a DB hiccup must NOT block a best-effort public read.
 */
async function resolvePolygonRpcUrl(): Promise<string> {
  try {
    const [row] = await db
      .select({ value: settings.value })
      .from(settings)
      .where(eq(settings.key, POLYGON_RPC_SETTINGS_KEY))
      .limit(1);
    const fromDb = coerceStringSetting(row?.value).trim();
    if (fromDb) return fromDb;
  } catch {
    // Fall through to env/default.
  }
  const fromEnv = (process.env.POLYECHO_POLYGON_RPC_URL ?? "").trim();
  return fromEnv || DEFAULT_POLYGON_RPC_URL;
}

/**
 * Parse the JSON-RPC `result` hex (0x-prefixed) into a 6-decimal USDC
 * USD number. `"0x"` and missing/empty are treated as zero. The BigInt
 * divmod path preserves sub-cent precision (Number(remainder)/1e6
 * avoids the float-loss that `Number(bigint)/1e6` would suffer for very
 * large balances).
 */
function parseUsdcHex(hexResult: unknown): number | null {
  if (typeof hexResult !== "string") return null;
  const raw = hexResult.trim();
  if (raw === "" || raw === "0x" || raw === "0X") return 0;
  if (!/^0x[0-9a-fA-F]+$/.test(raw)) return null;
  let units: bigint;
  try {
    units = BigInt(raw);
  } catch {
    return null;
  }
  if (units < 0n) return null;
  const divisor = 10n ** USDC_DECIMALS;
  const whole = units / divisor;
  const remainder = units % divisor;
  // Sub-cent precision: Number(remainder)/1e6 is exact for any 6-digit
  // remainder; Number(whole) loses precision only above 2^53, far above
  // any realistic wallet balance.
  const value = Number(whole) + Number(remainder) / 1_000_000;
  return Number.isFinite(value) ? value : null;
}

/**
 * probeIdleUsdc — best-effort, fail-soft on-chain read of an address's
 * idle USDC.e (Polygon) balance. Direct fetch (no proxy — dRPC is not
 * geo-blocked). Never throws; never writes; never logs.
 *
 * @param address 0x + 40 hex (validated with the shared EVM_ADDRESS_RE).
 */
export async function probeIdleUsdc(
  address: string
): Promise<ProbeIdleUsdcResult> {
  const failed: ProbeIdleUsdcResult = {
    ok: false,
    idleUsdc: null,
    message: t("tables.accounts.import.balanceIdleFailed"),
  };

  const addr = (address ?? "").trim();
  if (!EVM_ADDRESS_RE.test(addr)) return failed;

  try {
    const rpcUrl = await resolvePolygonRpcUrl();

    // Encode `balanceOf(address)` calldata: selector + 32-byte
    // zero-padded LOWERCASED address (no 0x prefix).
    const paddedAddr = addr.toLowerCase().slice(2).padStart(64, "0");
    const data = `${BALANCE_OF_SELECTOR}${paddedAddr}`;
    const payload = {
      jsonrpc: "2.0",
      method: "eth_call",
      params: [{ to: USDC_E_CONTRACT, data }, "latest"],
      id: 1,
    };

    const resp = await fetch(rpcUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(TIMEOUT_MS),
      cache: "no-store",
    });
    if (!resp.ok) return failed;

    const body: unknown = await resp.json();
    if (!body || typeof body !== "object") return failed;
    const result = (body as { result?: unknown }).result;
    if (result === undefined || result === null) return failed;

    const idleUsdc = parseUsdcHex(result);
    if (idleUsdc === null) return failed;

    return {
      ok: true,
      idleUsdc,
      message: t("tables.accounts.import.balanceIdleLabel"),
    };
  } catch {
    // Network / parse / timeout — strictly best-effort. Never logs.
    return failed;
  }
}

/**
 * probeTotalBalance — run the data-api positions probe (a) and the
 * on-chain idle-USDC.e probe (b) in PARALLEL, and compose a total. A
 * successful half is NEVER zeroed because the other half failed.
 *
 *   totalUsd = (positionsUsd ?? 0) + (idleUsdc ?? 0)
 *              when at least one is non-null, else null.
 *   ok       = at least one sub-probe succeeded.
 *
 * Never throws; never writes; never logs.
 */
export async function probeTotalBalance(
  address: string
): Promise<ProbeTotalBalanceResult> {
  const addr = (address ?? "").trim();
  if (!EVM_ADDRESS_RE.test(addr)) {
    return {
      ok: false,
      positionsUsd: null,
      idleUsdc: null,
      totalUsd: null,
      message: t("tables.accounts.import.balanceFailed"),
    };
  }

  const [posRes, idleRes] = await Promise.all([
    probeBalance(addr),
    probeIdleUsdc(addr),
  ]);

  const positionsUsd = posRes.valueUsd;
  const idleUsdc = idleRes.idleUsdc;
  const anyOk = posRes.ok || idleRes.ok;
  const totalUsd =
    positionsUsd === null && idleUsdc === null
      ? null
      : (positionsUsd ?? 0) + (idleUsdc ?? 0);

  let message: string;
  if (posRes.ok && idleRes.ok) {
    message = t("tables.accounts.import.balanceTotalLabel");
  } else if (anyOk) {
    message = t("tables.accounts.import.balancePartialFailed");
  } else {
    message = t("tables.accounts.import.balanceFailed");
  }

  return {
    ok: anyOk,
    positionsUsd,
    idleUsdc,
    totalUsd,
    message,
  };
}
