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
