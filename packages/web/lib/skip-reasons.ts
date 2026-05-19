/**
 * copy_skips.skip_reason taxonomy — mirrors
 * polyecho.shared.copytrading.SkipReason (StrEnum; the stored string IS the
 * enum value). Kept in sync by hand because the web package must not import
 * Python; the contract is frozen by packages/shared tests.
 *
 * Each entry maps a stored reason to a short human label and a Badge
 * variant so the operator can see WHY a copy was skipped at a glance.
 */

import { t } from "@/lib/i18n";

export type SkipBadgeVariant =
  | "default"
  | "secondary"
  | "outline"
  | "destructive";

interface SkipReasonMeta {
  /** Dict key under `tables.skipReasons.*` for the human label. */
  labelKey: import("@/lib/i18n").I18nKey;
  variant: SkipBadgeVariant;
}

/** Stored skip_reason -> { label key, badge variant }. */
export const SKIP_REASON_META: Record<string, SkipReasonMeta> = {
  dup: { labelKey: "tables.skipReasons.dup", variant: "outline" },
  not_following: {
    labelKey: "tables.skipReasons.notFollowing",
    variant: "secondary",
  },
  filter_keyword: {
    labelKey: "tables.skipReasons.filterKeyword",
    variant: "secondary",
  },
  filter_price_band: {
    labelKey: "tables.skipReasons.filterPriceBand",
    variant: "secondary",
  },
  filter_market_end: {
    labelKey: "tables.skipReasons.filterMarketEnd",
    variant: "secondary",
  },
  orderbook_spread: {
    labelKey: "tables.skipReasons.orderbookSpread",
    variant: "outline",
  },
  orderbook_depth: {
    labelKey: "tables.skipReasons.orderbookDepth",
    variant: "outline",
  },
  position_cap: {
    labelKey: "tables.skipReasons.positionCap",
    variant: "outline",
  },
  below_min_size: {
    labelKey: "tables.skipReasons.belowMinSize",
    variant: "secondary",
  },
  daily_order_cap: {
    labelKey: "tables.skipReasons.dailyOrderCap",
    variant: "outline",
  },
  daily_loss_cap: {
    labelKey: "tables.skipReasons.dailyLossCap",
    variant: "destructive",
  },
  portfolio_cap: {
    labelKey: "tables.skipReasons.portfolioCap",
    variant: "destructive",
  },
  slippage_reject: {
    labelKey: "tables.skipReasons.slippageReject",
    variant: "outline",
  },
  strategy_drift: {
    labelKey: "tables.skipReasons.strategyDrift",
    variant: "destructive",
  },
  data_unavailable: {
    labelKey: "tables.skipReasons.dataUnavailable",
    variant: "destructive",
  },
  live_locked: {
    labelKey: "tables.skipReasons.liveLocked",
    variant: "secondary",
  },
};

/**
 * Human label for a stored skip_reason; falls back to the raw value for
 * reasons outside the frozen taxonomy (the contract is frozen by
 * packages/shared tests, so an unknown value is shown verbatim).
 */
export function skipReasonLabel(reason: string): string {
  const meta = SKIP_REASON_META[reason];
  return meta ? t(meta.labelKey) : reason;
}

/** Badge variant for a stored skip_reason; unknown reasons stay neutral. */
export function skipReasonVariant(reason: string): SkipBadgeVariant {
  return SKIP_REASON_META[reason]?.variant ?? "secondary";
}
