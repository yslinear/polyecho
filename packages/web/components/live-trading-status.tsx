/**
 * <LiveTradingStatus /> — READ-ONLY live-trading lock indicator.
 *
 * PRD §5.7 操作分級: enabling/disabling live trading is HIGH-DANGER and is
 * CLI-ONLY. The web dashboard must NEVER render a button, switch, toggle or
 * form that turns live money on or off. This component is therefore a pure,
 * server-safe status pill — it only READS state and explains, in plain
 * copy, that the BINDING switch is the host env POLYECHO_LIVE_TRADING read
 * by trader-core. The `settings` row keyed `live_trading_ack` is the audit
 * record the CLI writes when the operator acknowledges live trading; we
 * surface it as supplementary context, never as the control.
 *
 * No "use client", no hooks, no interactivity by design.
 */

import { ShieldCheck, Lock } from "lucide-react";
import { cn } from "@/lib/utils";

export interface LiveTradingStatusProps {
  /**
   * Truthy when the `settings` row keyed `live_trading_ack` exists (the
   * CLI-written audit record). This is an INDICATOR only; it never grants
   * or revokes live trading.
   */
  acked: boolean;
  /** Compact pill for the app-shell header; verbose adds the explainer. */
  variant?: "pill" | "verbose";
  className?: string;
}

export function LiveTradingStatus({
  acked,
  variant = "pill",
  className,
}: LiveTradingStatusProps) {
  const locked = !acked;
  const label = locked ? "LOCKED" : "UNLOCKED";
  const Icon = locked ? Lock : ShieldCheck;

  const pill = (
    <span
      data-live-status={locked ? "locked" : "unlocked"}
      role="status"
      aria-label={`Live trading ${label}`}
      title={
        locked
          ? "Live trading is LOCKED. The binding switch is the host env POLYECHO_LIVE_TRADING (CLI only)."
          : "Live trading acknowledged in the audit log. The binding switch is still the host env POLYECHO_LIVE_TRADING (CLI only)."
      }
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold tabular-nums",
        locked
          ? "border-transparent bg-secondary text-secondary-foreground"
          : "border-transparent bg-primary text-primary-foreground",
        variant === "pill" && className
      )}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      Live {label}
    </span>
  );

  if (variant === "pill") return pill;

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex items-center gap-3">
        {pill}
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">
        This is a read-only indicator. The binding live-money switch is the
        host environment variable{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
          POLYECHO_LIVE_TRADING
        </code>{" "}
        read by trader-core, and it is changed on the CLI only (PRD §5.7
        high-danger tier). The dashboard intentionally offers no control to
        enable or disable live trading.
      </p>
    </div>
  );
}
