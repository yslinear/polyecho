/**
 * ScoreBadge — pill for numeric scores (0–100).
 *
 * Tier is conveyed by a TEXT label (High / Mid / Low), never by color alone
 * (DESIGN.md "Color never the sole signal"; PnL/score reinforcement rule).
 * Colors use DESIGN.md tokens only: --gain (High ≥ 70), --muted (Mid 40–69,
 * a calm neutral — there is no amber token and Mid is "not yet conviction",
 * not a warning), --loss (Low < 40), --muted for null/undefined/NaN.
 */

import { cn } from "@/lib/utils";

interface ScoreBadgeProps {
  label: string;
  value: string | number | null | undefined;
  className?: string;
}

type Tier = { tone: string; tier: string };

function scoreTier(v: number): Tier {
  if (v >= 70) return { tone: "bg-gain/15 text-gain", tier: "High" };
  if (v >= 40)
    return { tone: "bg-muted text-muted-foreground", tier: "Mid" };
  return { tone: "bg-loss/15 text-loss", tier: "Low" };
}

export function ScoreBadge({ label, value, className }: ScoreBadgeProps) {
  const num = value !== null && value !== undefined ? Number(value) : null;
  const known = num !== null && !isNaN(num);
  const { tone, tier } = known
    ? scoreTier(num as number)
    : { tone: "bg-muted text-muted-foreground", tier: "" };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold",
        tone,
        className
      )}
    >
      {label}
      {known ? (
        <>
          <span className="font-bold tabular-nums">
            {(num as number).toFixed(1)}
          </span>
          <span className="font-medium uppercase tracking-wide opacity-80">
            {tier}
          </span>
        </>
      ) : (
        <span className="opacity-70">Not set</span>
      )}
    </span>
  );
}
