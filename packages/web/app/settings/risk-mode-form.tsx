"use client";

/**
 * RiskModeForm — 旅行/保守模式 toggle (PRD §5.6).
 *
 * A single segmented control upserting the `risk_mode` settings row
 * ∈ normal | conservative (SAME bare-string convention as the proxy row).
 * trader-core reads it live: conservative tightens copy ratio + risk caps.
 * No modal — switching is safe and reversible; toast + router.refresh().
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Plane, Activity } from "lucide-react";

import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { setRiskMode } from "./actions";
import type { RiskMode } from "@/lib/settings-keys";

export function RiskModeForm({ current }: { current: RiskMode }) {
  const router = useRouter();
  const [mode, setMode] = React.useState<RiskMode>(current);
  const [pending, setPending] = React.useState(false);

  async function apply(next: RiskMode) {
    if (next === mode || pending) return;
    setPending(true);
    try {
      const res = await setRiskMode(next);
      if (res.ok) {
        setMode(res.value);
        toast.success(res.message);
        router.refresh();
      } else {
        toast.error(res.message);
      }
    } catch {
      toast.error(t("alertsSettings.settings.riskMode.errUpdateFailed"));
    } finally {
      setPending(false);
    }
  }

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      role="group"
      aria-label={t("alertsSettings.settings.riskMode.groupAria")}
    >
      <Button
        type="button"
        size="sm"
        variant={mode === "normal" ? "default" : "outline"}
        className="gap-1.5 text-xs"
        disabled={pending}
        aria-pressed={mode === "normal"}
        onClick={() => apply("normal")}
      >
        <Activity className="h-3.5 w-3.5" aria-hidden="true" />
        {t("alertsSettings.settings.riskMode.normal")}
      </Button>
      <Button
        type="button"
        size="sm"
        variant={mode === "conservative" ? "default" : "outline"}
        className="gap-1.5 text-xs"
        disabled={pending}
        aria-pressed={mode === "conservative"}
        onClick={() => apply("conservative")}
      >
        <Plane className="h-3.5 w-3.5" aria-hidden="true" />
        {t("alertsSettings.settings.riskMode.conservative")}
      </Button>
      <span
        className="text-xs text-muted-foreground"
        aria-live="polite"
        role="status"
      >
        {t("alertsSettings.settings.riskMode.current", {
          mode:
            mode === "conservative"
              ? t("alertsSettings.settings.riskMode.conservative")
              : t("alertsSettings.settings.riskMode.normal"),
        })}
      </span>
    </div>
  );
}
