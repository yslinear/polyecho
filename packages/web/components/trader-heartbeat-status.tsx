/**
 * <TraderHeartbeatStatus /> — READ-ONLY 跟單引擎存活指示 (PRD §7.4 / P1-5).
 *
 * trader-core's supervised loop upserts the `settings` row keyed
 * `trader_heartbeat` every cycle (and at startup). This component surfaces
 * that liveness as a calm status pill:
 *
 *   running → "跟單引擎：運行中 · 最後心跳 <relative>"
 *   stale   → "跟單引擎：停止 / 無心跳（> N 分鐘）"
 *   missing → "跟單引擎：無心跳"
 *
 * It only READS state — no controls, no hooks, no interactivity, mirroring
 * <LiveTradingStatus />. The binding switch for the engine is the host env
 * read by trader-core; the dashboard never starts/stops it.
 */

import { Activity, AlertTriangle } from "lucide-react";

import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { formatRelativeTime } from "@/lib/format";
import {
  type HeartbeatLiveness,
  type TraderHeartbeat,
  HEARTBEAT_STALE_MINUTES,
} from "@/lib/settings-keys";

export interface TraderHeartbeatStatusProps {
  /** Coerced heartbeat payload, or null when the row is absent/garbage. */
  heartbeat: TraderHeartbeat | null;
  /** Derived liveness verdict (running | stale | missing). */
  liveness: HeartbeatLiveness;
  className?: string;
}

export function TraderHeartbeatStatus({
  heartbeat,
  liveness,
  className,
}: TraderHeartbeatStatusProps) {
  const running = liveness === "running";
  const Icon = running ? Activity : AlertTriangle;

  let label: string;
  if (running && heartbeat) {
    label = t("common.heartbeat.running", {
      time: formatRelativeTime(heartbeat.ts),
    });
  } else if (liveness === "stale") {
    label = t("common.heartbeat.stale", {
      minutes: HEARTBEAT_STALE_MINUTES,
    });
  } else {
    label = t("common.heartbeat.missing");
  }

  return (
    <span
      data-heartbeat-status={liveness}
      role="status"
      aria-label={label}
      title={
        running
          ? t("common.heartbeat.titleRunning")
          : t("common.heartbeat.titleStale", {
              minutes: HEARTBEAT_STALE_MINUTES,
            })
      }
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium tabular-nums",
        running
          ? "border-transparent bg-secondary text-secondary-foreground"
          : "border-destructive/40 bg-destructive/10 text-destructive",
        className
      )}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}
