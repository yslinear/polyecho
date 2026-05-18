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
    label = `跟單引擎：運行中 · 最後心跳 ${formatRelativeTime(heartbeat.ts)}`;
  } else if (liveness === "stale") {
    label = `跟單引擎：停止 / 無心跳（> ${HEARTBEAT_STALE_MINUTES} 分鐘）`;
  } else {
    label = "跟單引擎：無心跳";
  }

  return (
    <span
      data-heartbeat-status={liveness}
      role="status"
      aria-label={label}
      title={
        running
          ? "跟單引擎正在運行。此為唯讀指示，無啟停控制。"
          : `跟單引擎無新心跳超過 ${HEARTBEAT_STALE_MINUTES} 分鐘。此為唯讀指示，無啟停控制。`
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
