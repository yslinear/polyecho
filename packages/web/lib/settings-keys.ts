/**
 * Shared `settings`-table key constants + coercion helpers.
 *
 * The settings row convention is bare-string/jsonb tolerant of `{"value":..}`
 * — IDENTICAL to how the Polymarket-proxy row is written in
 * app/settings/actions.ts. Python `polyecho.shared.risk_mode` reads
 * `risk_mode`; the global pause is read by trader-core's reconcile loop.
 *
 * Centralised so the Server Component reader and the Server Action writer can
 * never drift on key name or shape.
 */

/** §5.6 旅行/保守模式. Value ∈ "normal" | "conservative" (bare string). */
export const RISK_MODE_KEY = "risk_mode";

/** §5.8 全域暫停/恢復. Value "true" | "false" (bare string). */
export const GLOBAL_PAUSED_KEY = "global_paused";

/** §5.8 Web Push VAPID public key (base64url). Public — safe for the client. */
export const VAPID_PUBLIC_KEY = "vapid_public_key";

/**
 * §5.7 危險操作二次確認 — the operator-provisioned danger-tier passphrase
 * hash. Canonical FROZEN string `pbkdf2$<iters>$<b64salt>$<b64hash>` written
 * ONLY by the Python CLI (`polyecho account set-danger-passphrase`). The web
 * NEVER writes/derives this; it only READS it to verify a submitted
 * passphrase. Absence = fail-closed (no auto-provision).
 */
export const DANGER_PASSPHRASE_HASH_KEY = "danger_passphrase_hash";

/**
 * P1-5 跟單引擎心跳. trader-core's supervised loop upserts this `settings`
 * row every cycle (and at startup) with the live heartbeat payload. The web
 * is READ-ONLY here — it never writes this row, only surfaces liveness.
 */
export const TRADER_HEARTBEAT_KEY = "trader_heartbeat";

/**
 * The heartbeat payload trader-core writes (`Trader._write_heartbeat`).
 * Only `ts` is load-bearing for the Dashboard liveness pill; the rest is
 * supplementary context. Every field is optional/defensive because this is
 * an external producer and the row may be absent or partial.
 */
export interface TraderHeartbeat {
  /** ISO-8601 UTC timestamp of the most recent heartbeat. */
  ts: string;
  /** Loop phase, e.g. "startup" | "supervised" | "shutdown". */
  phase?: string;
  datasource?: string;
  live_trading?: boolean;
  detected?: number;
  copied?: number;
  skipped?: number;
  placed?: number;
  followed_leaders?: string[];
}

/**
 * Normalise the stored jsonb (object | {value:object} | null) to a
 * heartbeat with a valid `ts`, or null when the row is absent/garbage.
 * Mirrors the bare/`{value:..}` tolerance of `coerceStringSetting`.
 */
export function coerceHeartbeat(value: unknown): TraderHeartbeat | null {
  let obj: unknown = value;
  if (
    obj &&
    typeof obj === "object" &&
    "value" in obj &&
    typeof (obj as { value: unknown }).value === "object"
  ) {
    obj = (obj as { value: unknown }).value;
  }
  if (!obj || typeof obj !== "object") return null;
  const hb = obj as Record<string, unknown>;
  if (typeof hb.ts !== "string") return null;
  const t = new Date(hb.ts);
  if (Number.isNaN(t.getTime())) return null;
  return {
    ts: hb.ts,
    phase: typeof hb.phase === "string" ? hb.phase : undefined,
    datasource:
      typeof hb.datasource === "string" ? hb.datasource : undefined,
    live_trading:
      typeof hb.live_trading === "boolean" ? hb.live_trading : undefined,
    detected: typeof hb.detected === "number" ? hb.detected : undefined,
    copied: typeof hb.copied === "number" ? hb.copied : undefined,
    skipped: typeof hb.skipped === "number" ? hb.skipped : undefined,
    placed: typeof hb.placed === "number" ? hb.placed : undefined,
    followed_leaders: Array.isArray(hb.followed_leaders)
      ? hb.followed_leaders.filter((x): x is string => typeof x === "string")
      : undefined,
  };
}

/**
 * Liveness verdict for a heartbeat. "stale" when the last heartbeat is
 * older than the threshold; "missing" when there is no usable row.
 */
export type HeartbeatLiveness = "running" | "stale" | "missing";

/**
 * The supervised loop writes a heartbeat every cycle. We treat the engine
 * as DOWN when the newest heartbeat is older than this many minutes — a
 * generous multiple of a normal poll interval so a single slow cycle does
 * not flap the indicator.
 */
export const HEARTBEAT_STALE_MINUTES = 5;

/** Derive liveness from a coerced heartbeat against `now`. */
export function heartbeatLiveness(
  hb: TraderHeartbeat | null,
  now: Date = new Date(),
  staleMinutes: number = HEARTBEAT_STALE_MINUTES
): HeartbeatLiveness {
  if (hb === null) return "missing";
  const last = new Date(hb.ts).getTime();
  if (Number.isNaN(last)) return "missing";
  const ageMs = now.getTime() - last;
  return ageMs > staleMinutes * 60_000 ? "stale" : "running";
}

export type RiskMode = "normal" | "conservative";

/** Normalise stored jsonb (string | {value} | null) to a plain string. */
export function coerceStringSetting(value: unknown): string {
  if (typeof value === "string") return value;
  if (
    value &&
    typeof value === "object" &&
    "value" in value &&
    typeof (value as { value: unknown }).value === "string"
  ) {
    return (value as { value: string }).value;
  }
  return "";
}

/** Read a boolean settings row. Anything that is not exactly "true" === false. */
export function coerceBoolSetting(value: unknown): boolean {
  return coerceStringSetting(value).trim().toLowerCase() === "true";
}

/** Read the risk mode; unknown/empty falls back to the safe default "normal". */
export function coerceRiskMode(value: unknown): RiskMode {
  return coerceStringSetting(value).trim() === "conservative"
    ? "conservative"
    : "normal";
}
