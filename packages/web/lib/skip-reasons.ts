/**
 * copy_skips.skip_reason taxonomy — mirrors
 * polyecho.shared.copytrading.SkipReason (StrEnum; the stored string IS the
 * enum value). Kept in sync by hand because the web package must not import
 * Python; the contract is frozen by packages/shared tests.
 *
 * Each entry maps a stored reason to a short human label and a Badge
 * variant so the operator can see WHY a copy was skipped at a glance.
 */

export type SkipBadgeVariant =
  | "default"
  | "secondary"
  | "outline"
  | "destructive";

interface SkipReasonMeta {
  label: string;
  variant: SkipBadgeVariant;
}

/** Stored skip_reason -> { label, badge variant }. */
export const SKIP_REASON_META: Record<string, SkipReasonMeta> = {
  dup: { label: "Duplicate", variant: "outline" },
  not_following: { label: "Not following", variant: "secondary" },
  filter_keyword: { label: "Keyword filter", variant: "secondary" },
  filter_price_band: { label: "Price band", variant: "secondary" },
  filter_market_end: { label: "Market ending", variant: "secondary" },
  orderbook_spread: { label: "Spread too wide", variant: "outline" },
  orderbook_depth: { label: "Depth too thin", variant: "outline" },
  position_cap: { label: "Position cap", variant: "outline" },
  below_min_size: { label: "Below min size", variant: "secondary" },
  daily_order_cap: { label: "Daily order cap", variant: "outline" },
  daily_loss_cap: { label: "Daily loss cap", variant: "destructive" },
  portfolio_cap: { label: "Portfolio cap", variant: "destructive" },
  slippage_reject: { label: "Slippage reject", variant: "outline" },
  strategy_drift: { label: "Strategy drift", variant: "destructive" },
  data_unavailable: { label: "Data unavailable", variant: "destructive" },
  live_locked: { label: "Live locked", variant: "secondary" },
};

/** Human label for a stored skip_reason; falls back to the raw value. */
export function skipReasonLabel(reason: string): string {
  return SKIP_REASON_META[reason]?.label ?? reason;
}

/** Badge variant for a stored skip_reason; unknown reasons stay neutral. */
export function skipReasonVariant(reason: string): SkipBadgeVariant {
  return SKIP_REASON_META[reason]?.variant ?? "secondary";
}
