/**
 * <Pnl /> — the canonical money renderer for PolyEcho.
 *
 * Every PnL figure in the app MUST go through this component so the
 * gain/loss semantics are identical everywhere: a misread number is a bug
 * (PRODUCT.md "Legibility first"). Color is REINFORCEMENT ONLY — the sign
 * and the directional arrow carry the meaning, so the value stays legible
 * for color-blind users and in any theme (DESIGN.md).
 *
 * Server-safe: no "use client", no hooks, pure render. Drizzle returns
 * `numeric` columns as strings, so `string | number | null` is accepted
 * directly off a query row.
 */

import { cn } from "@/lib/utils";
import { toNum, formatUsd } from "@/lib/format";

export type PnlValue = number | string | null | undefined;

/**
 * "+$1,234.50" / "-$1,234.50" / "$0.00".
 * The sign is the primary signal; mirrors formatSignedUsd but is exported
 * here as the Pnl contract so downstream code formats consistently.
 */
export function formatPnl(value: PnlValue): string {
  const n = toNum(value) ?? 0;
  if (n > 0) return `+${formatUsd(n)}`;
  return formatUsd(n);
}

/** "up" for >0, "down" for <0, "flat" for 0 / null / unparseable. */
export function pnlDirection(value: PnlValue): "up" | "down" | "flat" {
  const n = toNum(value);
  if (n === null || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

const ARROW: Record<"up" | "down" | "flat", string> = {
  up: "↑", // ↑
  down: "↓", // ↓
  flat: "→", // → (neutral, no judgement)
};

export interface PnlProps {
  /** Raw PnL value; Drizzle numeric strings are accepted as-is. */
  value: PnlValue;
  /**
   * Render hint. "default" inline; "metric" for the dashboard focal figure
   * (larger weight). Sizing/typography is set by the caller via className;
   * this only toggles semantic emphasis.
   */
  emphasis?: "default" | "metric";
  /** Hide the directional arrow (e.g. very dense table cells). */
  hideArrow?: boolean;
  className?: string;
}

/**
 * Renders: <arrow> + signed USD, in tabular figures, tinted with
 * text-gain / text-loss / text-muted-foreground. The numeric text already
 * carries the sign so screen readers and mono-chrome displays are unambiguous.
 */
export function Pnl({
  value,
  emphasis = "default",
  hideArrow = false,
  className,
}: PnlProps) {
  const dir = pnlDirection(value);
  const tone =
    dir === "up"
      ? "text-gain"
      : dir === "down"
        ? "text-loss"
        : "text-muted-foreground";

  return (
    <span
      className={cn(
        "tabular-nums",
        emphasis === "metric" ? "font-semibold" : "font-medium",
        tone,
        className,
      )}
      data-pnl={dir}
    >
      {!hideArrow && (
        <span aria-hidden="true" className="mr-0.5">
          {ARROW[dir]}
        </span>
      )}
      {formatPnl(value)}
    </span>
  );
}
