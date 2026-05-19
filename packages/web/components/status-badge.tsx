/**
 * <StatusBadge /> — the calm verdict pill used by /status.
 *
 * Renders the shared {@link StatusPresentation}: a shadcn Badge (variant =
 * colour reinforcement) carrying a Lucide icon + a plain text label. The
 * label is the load-bearing signal; icon + colour only reinforce it
 * (DESIGN.md "Color never the sole signal", colour-blind safe). READ-ONLY,
 * no interactivity — it mirrors the existing <TraderHeartbeatStatus /> idiom.
 */

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { StatusPresentation } from "@/lib/status";

export interface StatusBadgeProps {
  presentation: StatusPresentation;
  className?: string;
}

export function StatusBadge({ presentation, className }: StatusBadgeProps) {
  const { label, variant, Icon, token } = presentation;
  return (
    <Badge
      variant={variant}
      data-status={token}
      role="status"
      aria-label={label}
      className={cn("gap-1.5 font-medium", className)}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </Badge>
  );
}
