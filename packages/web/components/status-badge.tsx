/**
 * <StatusBadge /> — the calm verdict pill used by /status.
 *
 * Renders the shared {@link StatusPresentation} as a calm verdict pill. The
 * meaning is carried by FOUR redundant channels and the first two are
 * sufficient on their own: a plain text label (load-bearing) + a Lucide icon
 * (distinct SHAPE per state). The shadcn Badge `variant` and the semantic
 * `tone` colour are pure reinforcement — a colour-blind operator or a
 * greyscale screenshot loses nothing (DESIGN.md "Color never the sole
 * signal", colour-blind safe).
 *
 * `tone` resolves to a soft-tint pill (token-driven, no raw hex / no
 * Tailwind palette literal): a low-chroma brand-tinted fill + an AA
 * foreground + a matching hairline border, tuned for light AND dark in
 * app/globals.css. `neutral` deliberately adds NO colour — it inherits the
 * Badge's plain `outline` look so "no data" never reads as an alarm. The
 * tint classes come AFTER the variant base so tailwind-merge lets them win
 * without touching the shared ui/Badge variant union.
 * READ-ONLY, no interactivity — mirrors the <TraderHeartbeatStatus /> idiom.
 */

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { StatusPresentation, StatusTone } from "@/lib/status";

/**
 * tone → soft-tint pill classes. Placed after the Badge variant base so
 * tailwind-merge overrides `secondary`/`destructive`'s fill, border and the
 * leftover `hover:bg-*` (the pill is read-only — hover must not flash the
 * old variant colour). `neutral` adds nothing: it keeps the plain `outline`
 * Badge so "no data" never reads as an alarm.
 */
const TONE_CLASS: Record<StatusTone, string> = {
  ok: "border-status-ok-border bg-status-ok text-status-ok-foreground hover:bg-status-ok",
  warn: "border-status-warn-border bg-status-warn text-status-warn-foreground hover:bg-status-warn",
  down: "border-status-down-border bg-status-down text-status-down-foreground hover:bg-status-down",
  neutral: "",
};

export interface StatusBadgeProps {
  presentation: StatusPresentation;
  className?: string;
}

export function StatusBadge({ presentation, className }: StatusBadgeProps) {
  const { label, variant, Icon, tone, token } = presentation;
  return (
    <Badge
      variant={variant}
      data-status={token}
      data-tone={tone}
      role="status"
      aria-label={label}
      className={cn("gap-1.5 font-medium", TONE_CLASS[tone], className)}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </Badge>
  );
}
