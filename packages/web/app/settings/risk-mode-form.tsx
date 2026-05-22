"use client";

/**
 * RiskModeForm — 旅行/保守模式 toggle (PRD §5.6).
 *
 * P7d / R24 asymmetry:
 *   - "conservative" (tighter caps) is safe-direction → single-click.
 *   - "normal" (LEAVE conservative, loosen caps) is risk-increasing → must
 *     pass the inline-confirm primitive (CONFIRM_PHRASE only, no passphrase).
 *
 * Both controls render side-by-side; when the current mode is conservative
 * the "normal" affordance becomes the inline-confirm trigger (still inline,
 * not a modal — DESIGN.md ban).
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Plane, Activity } from "lucide-react";

import { Button } from "@/components/ui/button";
import { InlineConfirm } from "@/components/inline-confirm";
import { t } from "@/lib/i18n";
import { setRiskMode } from "./actions";
import type { RiskMode } from "@/lib/settings-keys";

export function RiskModeForm({ current }: { current: RiskMode }) {
  const router = useRouter();
  const [mode, setMode] = React.useState<RiskMode>(current);
  const [pending, setPending] = React.useState(false);

  async function applyConservative() {
    if (mode === "conservative" || pending) return;
    setPending(true);
    try {
      const res = await setRiskMode("conservative");
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
      {mode === "normal" ? (
        <Button
          type="button"
          size="sm"
          variant="default"
          className="gap-1.5 text-xs"
          disabled
          aria-pressed={true}
        >
          <Activity className="h-3.5 w-3.5" aria-hidden="true" />
          {t("alertsSettings.settings.riskMode.normal")}
        </Button>
      ) : (
        // Risk-increasing: leave conservative → must inline-confirm.
        <InlineConfirm
          title={t("alertsSettings.settings.riskMode.normal")}
          description={t(
            "alertsSettings.settings.riskMode.confirmNormalDescription"
          )}
          requirePassphrase={false}
          triggerVariant="default"
          triggerClassName="h-8 gap-1.5 text-xs"
          successToast={t(
            "alertsSettings.settings.riskMode.normalSuccessToast"
          )}
          action={async ({ confirmText }) => {
            const res = await setRiskMode("normal", { confirmText });
            return res;
          }}
          onConfirmed={() => setMode("normal")}
        />
      )}
      <Button
        type="button"
        size="sm"
        variant={mode === "conservative" ? "default" : "outline"}
        className="gap-1.5 text-xs"
        disabled={pending || mode === "conservative"}
        aria-pressed={mode === "conservative"}
        onClick={applyConservative}
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
