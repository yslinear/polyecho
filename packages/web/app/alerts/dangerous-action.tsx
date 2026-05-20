"use client";

/**
 * DangerousAction — INLINE single-step confirm for §5.7 危险-tier ops.
 *
 * The operator decided the elaborate "create a pending request, then
 * confirm it separately" UI is unnecessary. A dangerous op now requires an
 * INLINE repeated confirmation in ONE place, then executes directly:
 *
 *   1. Operator picks MANUAL_SELL / EMERGENCY_LIQUIDATE and fills params.
 *   2. In the SAME inline form they must (a) type the verbatim confirm
 *      phrase {@link CONFIRM_PHRASE} AND (b) enter the operator danger
 *      passphrase. These two independent inputs are the anti-misclick +
 *      authentication factors (they replace the old separate pending row
 *      + 5-second countdown gate, which only existed to guard the
 *      two-step UI).
 *   3. Submitting calls `confirmDangerousAction` — ONE authenticated
 *      Server Action that verifies the passphrase (constant-time, against
 *      the operator-provisioned `settings.danger_passphrase_hash`,
 *      fail-closed) and ONLY THEN writes a `confirmed` pending_actions row
 *      trader-core executes. A wrong passphrase writes NOTHING.
 *
 * Deliberately NOT a modal (DESIGN.md bans modal-first) — it is an inline
 * disclosure under a clearly labelled, dashed-border danger section.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { t } from "@/lib/i18n";
import { confirmDangerousAction } from "./actions";
import { CONFIRM_PHRASE, type PendingActionType } from "./dangerous-shared";

export function DangerousActionInline() {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [actionType, setActionType] =
    React.useState<PendingActionType>("manual_sell");
  const [target, setTarget] = React.useState("");
  const [size, setSize] = React.useState("");
  const [confirmText, setConfirmText] = React.useState("");
  const [passphrase, setPassphrase] = React.useState("");
  const [pending, setPending] = React.useState(false);

  function reset() {
    setTarget("");
    setSize("");
    setConfirmText("");
    setPassphrase("");
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const targetValue = target.trim();
    if (!targetValue) {
      toast.error(t("alertsSettings.alerts.danger.errNoTarget"));
      return;
    }
    const params: Record<string, unknown> = { target: targetValue };
    if (actionType === "manual_sell") {
      const s = size.trim();
      if (!s) {
        toast.error(t("alertsSettings.alerts.danger.errNoSize"));
        return;
      }
      params.size = s;
    }
    if (confirmText.trim() !== CONFIRM_PHRASE) {
      toast.error(
        t("alertsSettings.alerts.danger.errConfirmPhrase", {
          phrase: CONFIRM_PHRASE,
        })
      );
      return;
    }
    if (passphrase.length < 1) {
      toast.error(t("alertsSettings.alerts.danger.errNoPassphrase"));
      return;
    }
    setPending(true);
    try {
      const res = await confirmDangerousAction({
        actionType,
        params,
        confirmText,
        passphrase,
      });
      if (res.ok) {
        toast.success(res.message);
        reset();
        setOpen(false);
        router.refresh();
      } else {
        toast.error(res.message);
        // A wrong passphrase wrote nothing — clear only the secrets so the
        // operator can re-enter without rebuilding the whole request.
        setConfirmText("");
        setPassphrase("");
      }
    } catch {
      toast.error(t("alertsSettings.alerts.danger.errConfirmFailed"));
    } finally {
      setPending(false);
    }
  }

  if (!open) {
    return (
      <Button
        variant="outline"
        size="sm"
        className="text-xs"
        onClick={() => setOpen(true)}
      >
        <AlertTriangle
          className="mr-1.5 h-3.5 w-3.5 text-loss"
          aria-hidden="true"
        />
        {t("alertsSettings.alerts.danger.openButton")}
      </Button>
    );
  }

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-4 rounded-lg border border-dashed border-loss/50 p-4"
      aria-label={t("alertsSettings.alerts.danger.formAria")}
    >
      <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
        <AlertTriangle
          className="mt-0.5 h-3.5 w-3.5 shrink-0 text-loss"
          aria-hidden="true"
        />
        <span>{t("alertsSettings.alerts.danger.formIntro")}</span>
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label
            htmlFor="da-type"
            className="text-sm font-medium leading-none"
          >
            {t("alertsSettings.alerts.danger.typeLabel")}
          </label>
          <Select
            value={actionType}
            onValueChange={(v) => setActionType(v as PendingActionType)}
          >
            <SelectTrigger id="da-type" className="text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="manual_sell">
                {t("alertsSettings.alerts.danger.typeManualSell")}
              </SelectItem>
              <SelectItem value="emergency_liquidate">
                {t("alertsSettings.alerts.danger.typeEmergencyLiquidate")}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="da-target"
            className="text-sm font-medium leading-none"
          >
            {t("alertsSettings.alerts.danger.targetLabel")}
          </label>
          <Input
            id="da-target"
            value={target}
            spellCheck={false}
            autoComplete="off"
            placeholder={t("alertsSettings.alerts.danger.targetPlaceholder")}
            className="font-mono text-sm"
            onChange={(e) => setTarget(e.target.value)}
          />
        </div>

        {actionType === "manual_sell" ? (
          <div className="space-y-1.5">
            <label
              htmlFor="da-size"
              className="text-sm font-medium leading-none"
            >
              {t("alertsSettings.alerts.danger.sizeLabel")}
            </label>
            <Input
              id="da-size"
              inputMode="decimal"
              value={size}
              autoComplete="off"
              placeholder={t("alertsSettings.alerts.danger.sizePlaceholder")}
              className="font-mono text-sm tabular-nums"
              onChange={(e) => setSize(e.target.value)}
            />
          </div>
        ) : null}
      </div>

      {/* Inline repeated confirmation: type-to-confirm + danger passphrase.
          Both are required by the Server Action; this is the whole gate. */}
      <div className="space-y-4 rounded-md border border-loss/30 bg-loss/5 p-3">
        <p className="text-xs font-medium text-loss">
          {t("alertsSettings.alerts.danger.confirmSectionTitle")}
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label
              htmlFor="da-confirm"
              className="text-sm font-medium leading-none"
            >
              {t("alertsSettings.alerts.danger.confirmPhraseLabel", {
                phrase: CONFIRM_PHRASE,
              })}
            </label>
            <Input
              id="da-confirm"
              value={confirmText}
              autoComplete="off"
              spellCheck={false}
              placeholder={CONFIRM_PHRASE}
              className="h-9 font-mono text-sm"
              onChange={(e) => setConfirmText(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <label
              htmlFor="da-pass"
              className="text-sm font-medium leading-none"
            >
              {t("alertsSettings.alerts.danger.passphraseLabel")}
            </label>
            <Input
              id="da-pass"
              type="password"
              value={passphrase}
              autoComplete="off"
              spellCheck={false}
              placeholder={t(
                "alertsSettings.alerts.danger.passphrasePlaceholder"
              )}
              className="h-9 font-mono text-sm"
              onChange={(e) => setPassphrase(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {t("alertsSettings.alerts.danger.passphraseHint")}
            </p>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Button
          type="submit"
          size="sm"
          variant="destructive"
          disabled={pending}
          aria-label={t("alertsSettings.alerts.danger.confirmAria")}
        >
          {pending
            ? t("alertsSettings.alerts.danger.confirming")
            : t("alertsSettings.alerts.danger.confirmSubmit")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={pending}
          onClick={() => {
            reset();
            setOpen(false);
          }}
        >
          {t("alertsSettings.alerts.danger.cancel")}
        </Button>
      </div>
    </form>
  );
}
