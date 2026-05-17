/**
 * ScoreBadge — coloured pill for numeric scores (0–100).
 * Green ≥ 70, yellow 40–69, red < 40, grey for null/undefined.
 */

import { cn } from "@/lib/utils";

interface ScoreBadgeProps {
  label: string;
  value: string | number | null | undefined;
  className?: string;
}

function scoreColor(v: number): string {
  if (v >= 70) return "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300";
  if (v >= 40) return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300";
  return "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300";
}

export function ScoreBadge({ label, value, className }: ScoreBadgeProps) {
  const num = value !== null && value !== undefined ? Number(value) : null;
  const color =
    num !== null && !isNaN(num)
      ? scoreColor(num)
      : "bg-muted text-muted-foreground";

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold",
        color,
        className
      )}
    >
      {label}
      {num !== null && !isNaN(num) ? (
        <span className="font-bold">{num.toFixed(1)}</span>
      ) : (
        <span className="opacity-60">—</span>
      )}
    </span>
  );
}
