/**
 * TemplateConfig risk linter (PolyHermes deep-dive B3).
 *
 * PolyEcho's 16-step trade-time risk gate enforces caps when an order is
 * placed. PolyHermes additionally ships `inspectRiskConfig`, a *pre-flight*
 * advisor that flags a copy-trade template whose risk caps are missing or
 * dangerously loose BEFORE an operator enables a live follow. This module is
 * that advisor on the web side.
 *
 * It mirrors `polyecho.core.template_linter` (Python). The web package must
 * not import Python, so this is a hand-kept port; both sides lint the same
 * caps with the same thresholds. The web `copy_templates` table only stores
 * three of the B3 caps (`max_position_usd`, `daily_loss_limit_usd`,
 * `max_concurrent_positions`) — the Python `TemplateConfig` carries the full
 * set including `open_loss_limit_usd` / `portfolio_max_usd`, which live only
 * in the engine's in-memory snapshot. This linter checks the columns the DB
 * actually exposes; no schema change.
 *
 * Pure: a function of its input, no I/O, no side effects. A well-configured
 * template yields an empty array.
 */

import type { I18nKey } from "@/lib/i18n";

/** Warning severity. `high` = a money-loss cap can never fire. */
export type TemplateWarningSeverity = "high" | "medium";

/**
 * The subset of `copy_templates` columns this linter inspects. Numeric
 * columns arrive from Drizzle as strings (Postgres `numeric`); `null` means
 * unset. Callers pass the raw row fields verbatim.
 */
export interface LintableTemplate {
  max_position_usd: string | number | null;
  daily_loss_limit_usd: string | number | null;
  max_concurrent_positions: number | null;
}

/** One advisory finding about a template's risk configuration. */
export interface TemplateWarning {
  /** Stable machine code; also the i18n-key seam (`tables.leaders.templateRisk.codes.*`). */
  code: string;
  /** The offending `copy_templates` column. */
  field: string;
  severity: TemplateWarningSeverity;
  /** i18n key for the human advisory message. */
  messageKey: I18nKey;
  /** Suggested value rendered for the operator (string keeps int/decimal uniform). */
  suggested: string;
}

// ---------------------------------------------------------------------------
// Thresholds — kept byte-for-byte aligned with template_linter.py.
//
// LOOSE_*  : a cap ABOVE this is "loose" (set, but weak protection).
// SUGGEST_*: the modest default proposed when a cap is missing entirely.
// ---------------------------------------------------------------------------
export const LOOSE_MAX_POSITION_USD = 5000;
export const LOOSE_DAILY_LOSS_LIMIT_USD = 2000;
export const LOOSE_MAX_CONCURRENT_POSITIONS = 25;

export const SUGGEST_MAX_POSITION_USD = 500;
export const SUGGEST_DAILY_LOSS_LIMIT_USD = 200;
export const SUGGEST_MAX_CONCURRENT_POSITIONS = 5;

/** Parse a Drizzle numeric/int field to a finite number, or null if unset. */
function toNumber(value: string | number | null): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/**
 * Inspect one template and return ordered advisory warnings. Pure. A clean
 * template yields `[]`. `high` warnings sort ahead of `medium`; within a
 * severity, missing caps precede loose ones (matches the Python ordering).
 */
export function lintTemplateConfig(
  tpl: LintableTemplate,
): TemplateWarning[] {
  const missing: TemplateWarning[] = [];
  const loose: TemplateWarning[] = [];

  const maxPos = toNumber(tpl.max_position_usd);
  const dailyLoss = toNumber(tpl.daily_loss_limit_usd);
  const concurrency =
    tpl.max_concurrent_positions === null
      ? null
      : Number(tpl.max_concurrent_positions);

  // --- missing caps -------------------------------------------------------
  if (maxPos === null) {
    missing.push({
      code: "missing.max_position_usd",
      field: "max_position_usd",
      severity: "high",
      messageKey: "tables.leaders.templateRisk.codes.missingMaxPosition",
      suggested: String(SUGGEST_MAX_POSITION_USD),
    });
  }
  if (dailyLoss === null) {
    missing.push({
      code: "missing.daily_loss_limit_usd",
      field: "daily_loss_limit_usd",
      severity: "high",
      messageKey: "tables.leaders.templateRisk.codes.missingDailyLoss",
      suggested: String(SUGGEST_DAILY_LOSS_LIMIT_USD),
    });
  }
  if (concurrency === null || !Number.isFinite(concurrency)) {
    missing.push({
      code: "missing.max_concurrent_positions",
      field: "max_concurrent_positions",
      severity: "medium",
      messageKey: "tables.leaders.templateRisk.codes.missingConcurrency",
      suggested: String(SUGGEST_MAX_CONCURRENT_POSITIONS),
    });
  }

  // --- loose caps ---------------------------------------------------------
  if (maxPos !== null && maxPos > LOOSE_MAX_POSITION_USD) {
    loose.push({
      code: "loose.max_position_usd",
      field: "max_position_usd",
      severity: "medium",
      messageKey: "tables.leaders.templateRisk.codes.looseMaxPosition",
      suggested: String(LOOSE_MAX_POSITION_USD),
    });
  }
  if (dailyLoss !== null && dailyLoss > LOOSE_DAILY_LOSS_LIMIT_USD) {
    loose.push({
      code: "loose.daily_loss_limit_usd",
      field: "daily_loss_limit_usd",
      severity: "medium",
      messageKey: "tables.leaders.templateRisk.codes.looseDailyLoss",
      suggested: String(LOOSE_DAILY_LOSS_LIMIT_USD),
    });
  }
  if (
    concurrency !== null &&
    Number.isFinite(concurrency) &&
    concurrency > LOOSE_MAX_CONCURRENT_POSITIONS
  ) {
    loose.push({
      code: "loose.max_concurrent_positions",
      field: "max_concurrent_positions",
      severity: "medium",
      messageKey: "tables.leaders.templateRisk.codes.looseConcurrency",
      suggested: String(LOOSE_MAX_CONCURRENT_POSITIONS),
    });
  }

  // HIGH before MEDIUM; insertion order preserved within a severity.
  return [...missing, ...loose].sort(
    (a, b) =>
      (a.severity === "high" ? 0 : 1) - (b.severity === "high" ? 0 : 1),
  );
}

/** True if any warning is `high` (a money-loss cap is off). */
export function hasHighSeverity(warnings: TemplateWarning[]): boolean {
  return warnings.some((w) => w.severity === "high");
}
