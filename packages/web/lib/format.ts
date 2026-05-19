/**
 * Shared display formatters for read-only Dashboard pages (Phase 3).
 *
 * Drizzle returns `numeric` columns as strings and `timestamp` columns as
 * `Date`. These helpers turn nullable DB values into stable, test-friendly
 * display strings — no locale surprises, no `any`.
 *
 * User-facing fallbacks ("not set" / "not recorded" / "unknown") render in
 * tables and cards, so they go through the sync `t()` (Server+Client safe).
 * The timezone / number logic below is locale-independent and unchanged.
 */
import { t } from "@/lib/i18n";

/** Parse a Drizzle numeric (string | number | null) into a finite number or null. */
export function toNum(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Sum a list of nullable numeric values, ignoring nulls. */
export function sumPnl(
  values: ReadonlyArray<string | number | null | undefined>
): number {
  return values.reduce<number>((acc, v) => acc + (toNum(v) ?? 0), 0);
}

/** Format a USD amount, e.g. 1234.5 -> "$1,234.50"; null -> "$0.00". */
export function formatUsd(v: string | number | null | undefined): string {
  const n = toNum(v) ?? 0;
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${sign}$${abs}`;
}

/** Signed USD with explicit + for positive PnL; null -> "$0.00". */
export function formatSignedUsd(v: string | number | null | undefined): string {
  const n = toNum(v) ?? 0;
  if (n > 0) return `+${formatUsd(n)}`;
  return formatUsd(n);
}

/**
 * Tailwind text colour class for a PnL value.
 * Uses DESIGN.md tokens (`text-gain`/`text-loss`); colour is reinforcement
 * only — callers also render sign + arrow (color-blind safe).
 */
export function pnlColor(v: string | number | null | undefined): string {
  const n = toNum(v);
  if (n === null || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-gain" : "text-loss";
}

/** Shorten a 0x address: 0x1234…abcd. Returns input unchanged if too short. */
export function shortAddress(addr: string | null | undefined): string {
  if (!addr) return t("tables.common.notSet");
  if (addr.length <= 12) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

/**
 * EVM address shape: `0x` + 40 hex chars (case-insensitive). Shared by the
 * discovery Server Action's input guard and the web key-import flow — a
 * Server Action is a public RPC, so an arbitrary / unbounded string must
 * never reach the DB (rev-web R16). Shape-only; does NOT checksum.
 */
export const EVM_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

/**
 * secp256k1 private key as the CLI accepts it: an optionally-`0x`-prefixed
 * 64-hex (32-byte) string. Shape-only — the trimmed string is stored
 * verbatim (the CLI does not re-encode) so trader-core round-trips it back.
 */
export const PRIVATE_KEY_RE = /^(0x)?[0-9a-fA-F]{64}$/;

/** ISO-ish date string (YYYY-MM-DD HH:mm) from a Date | null, UTC, stable. */
export function formatDateTime(v: Date | string | null | undefined): string {
  if (!v) return t("tables.common.notRecorded");
  const d = v instanceof Date ? v : new Date(v);
  if (Number.isNaN(d.getTime())) return t("tables.common.notRecorded");
  const pad = (x: number) => String(x).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    ` ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  );
}

/** Date-only (YYYY-MM-DD), UTC. */
export function formatDate(v: Date | string | null | undefined): string {
  if (!v) return t("tables.common.notRecorded");
  const d = v instanceof Date ? v : new Date(v);
  if (Number.isNaN(d.getTime())) return t("tables.common.notRecorded");
  const pad = (x: number) => String(x).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(
    d.getUTCDate()
  )}`;
}

/**
 * Human label for accounts.signature_type.
 * 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271 (PRD §6.1).
 */
export function signatureTypeLabel(
  code: number | null | undefined
): string {
  switch (code) {
    case 0:
      return "EOA";
    case 1:
      return "POLY_PROXY";
    case 2:
      return "POLY_GNOSIS_SAFE";
    case 3:
      return "POLY_1271";
    default:
      return t("common.unknown");
  }
}

/** True when a my_trades row represents an open position. */
export function isOpenPosition(row: {
  opened_at: Date | string | null;
  closed_at: Date | string | null;
}): boolean {
  return row.opened_at != null && row.closed_at == null;
}

/** Returns the start-of-today Date in UTC (00:00:00.000). */
export function startOfTodayUtc(now: Date = new Date()): Date {
  return new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  );
}

/**
 * Compact zh-TW relative time, past only (e.g. "12 秒前", "3 分鐘前",
 * "2 小時前", "5 天前"). Used by the bot-heartbeat pill. Returns
 * the not-recorded fallback for unparseable input. No em dashes, no
 * locale surprises.
 */
export function formatRelativeTime(
  v: Date | string | null | undefined,
  now: Date = new Date()
): string {
  if (!v) return t("tables.common.notRecorded");
  const d = v instanceof Date ? v : new Date(v);
  if (Number.isNaN(d.getTime())) return t("tables.common.notRecorded");
  const secs = Math.max(0, Math.floor((now.getTime() - d.getTime()) / 1000));
  if (secs < 60) return `${secs} 秒前`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} 分鐘前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} 小時前`;
  const days = Math.floor(hours / 24);
  return `${days} 天前`;
}

/** Sum pnl_usd only for trades whose opened_at falls on/after `since`. */
export function sumPnlSince(
  rows: ReadonlyArray<{
    pnl_usd: string | number | null;
    opened_at: Date | string | null;
  }>,
  since: Date
): number {
  return rows.reduce<number>((acc, r) => {
    if (!r.opened_at) return acc;
    const t = r.opened_at instanceof Date ? r.opened_at : new Date(r.opened_at);
    if (Number.isNaN(t.getTime()) || t < since) return acc;
    return acc + (toNum(r.pnl_usd) ?? 0);
  }, 0);
}
