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
import { t } from "@/lib/i18n";
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
  const label = locked
    ? t("common.liveTrading.locked")
    : t("common.liveTrading.unlocked");
  const Icon = locked ? Lock : ShieldCheck;

  const pill = (
    <span
      data-live-status={locked ? "locked" : "unlocked"}
      role="status"
      aria-label={t("common.liveTrading.pillLabel", { state: label })}
      title={
        locked
          ? t("common.liveTrading.titleLocked")
          : t("common.liveTrading.titleUnlocked")
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
      {t("common.liveTrading.pillText", { state: label })}
    </span>
  );

  if (variant === "pill") return pill;

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex items-center gap-3">
        {pill}
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">
        {t("common.liveTrading.explainerBefore")}
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
          {/* Environment-variable name — proper noun per common.ts
              PROPER-NOUN POLICY, not translatable copy. */}
          {"POLYECHO_LIVE_TRADING"}
        </code>
        {t("common.liveTrading.explainerAfter")}
      </p>
    </div>
  );
}
