"use client";

/**
 * DangerousAction — create + two-step confirm for §5.7 危险-tier ops.
 *
 * Flow (PRD §5.7):
 *   1. Operator picks MANUAL_SELL / EMERGENCY_LIQUIDATE and fills params
 *      -> createPendingAction (status awaiting_confirm, 300s TTL).
 *   2. A pending row appears in the panel with a hard 5-second countdown
 *      GATE; only after it elapses does the passphrase input enable.
 *   3. Passphrase -> confirmPendingAction (web hashes + verifies; flips
 *      status to confirmed). Wrong passphrase keeps the row (TTL expires it).
 *
 * The countdown + passphrase live on the pending-row card in
 * <PendingActionsPanel> (passed in by the /alerts Server Component). This
 * component is only the CREATE form — deliberately not a modal (PRD bans
 * modal-first); it's an inline disclosure under a clearly labelled section.
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
import {
  createPendingAction,
  type PendingActionType,
} from "./actions";

export function CreateDangerousAction() {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [actionType, setActionType] =
    React.useState<PendingActionType>("manual_sell");
  const [target, setTarget] = React.useState("");
  const [size, setSize] = React.useState("");
  const [pending, setPending] = React.useState(false);

  async function onCreate(e: React.FormEvent<HTMLFormElement>) {
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
    setPending(true);
    try {
      const res = await createPendingAction(actionType, params);
      if (res.ok) {
        toast.success(res.message);
        setTarget("");
        setSize("");
        setOpen(false);
        router.refresh();
      } else {
        toast.error(res.message);
      }
    } catch {
      toast.error(t("alertsSettings.alerts.danger.errCreateFailed"));
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
      onSubmit={onCreate}
      className="space-y-4 rounded-lg border border-dashed p-4"
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

      <div className="flex items-center gap-3">
        <Button type="submit" size="sm" disabled={pending}>
          {pending
            ? t("alertsSettings.alerts.danger.creating")
            : t("alertsSettings.alerts.danger.createSubmit")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={pending}
          onClick={() => setOpen(false)}
        >
          {t("alertsSettings.alerts.danger.cancel")}
        </Button>
      </div>
    </form>
  );
}

/**
 * ConfirmPendingRow — the 5s countdown gate + passphrase confirm for ONE
 * awaiting_confirm row. Rendered by the server-side PendingActionsPanel.
 *
 * `gateSeconds` defaults to 5 (PRD §5.7 mandates the 5-second gate); it is
 * injectable purely so the countdown is deterministically testable without
 * fake-timer gymnastics — production callers never pass it.
 */
export function ConfirmPendingRow({
  id,
  expiresAtIso,
  gateSeconds = 5,
}: {
  id: string;
  /** ISO string of pending_actions.expires_at */
  expiresAtIso: string | null;
  /** Countdown length in seconds. Default 5 (PRD §5.7). */
  gateSeconds?: number;
}) {
  const router = useRouter();
  const [secondsLeft, setSecondsLeft] = React.useState(gateSeconds);
  const [passphrase, setPassphrase] = React.useState("");
  const [pending, setPending] = React.useState(false);

  // Hard countdown gate before the passphrase input is usable.
  React.useEffect(() => {
    if (secondsLeft <= 0) return;
    const t = setTimeout(() => setSecondsLeft((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [secondsLeft]);

  const expired =
    expiresAtIso != null &&
    new Date(expiresAtIso).getTime() <= Date.now();
  const gateOpen = secondsLeft <= 0 && !expired;

  async function onConfirm(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!gateOpen) return;
    if (passphrase.length < 1) {
      toast.error(t("alertsSettings.alerts.danger.errNoPassphrase"));
      return;
    }
    setPending(true);
    try {
      const { confirmPendingAction } = await import("./actions");
      const res = await confirmPendingAction(id, passphrase);
      if (res.ok) {
        toast.success(res.message);
        setPassphrase("");
        router.refresh();
      } else {
        toast.error(res.message);
        setPassphrase("");
      }
    } catch {
      toast.error(t("alertsSettings.alerts.danger.errConfirmFailed"));
    } finally {
      setPending(false);
    }
  }

  if (expired) {
    return (
      <p className="text-xs text-muted-foreground">
        {t("alertsSettings.alerts.danger.expired")}
      </p>
    );
  }

  return (
    <form onSubmit={onConfirm} className="flex flex-wrap items-end gap-3">
      <div className="space-y-1.5">
        <label
          htmlFor={`pp-${id}`}
          className="text-xs font-medium leading-none text-muted-foreground"
        >
          {t("alertsSettings.alerts.danger.passphraseLabel")}
        </label>
        <Input
          id={`pp-${id}`}
          type="password"
          value={passphrase}
          autoComplete="off"
          disabled={!gateOpen || pending}
          placeholder={
            gateOpen
              ? t("alertsSettings.alerts.danger.passphrasePlaceholder")
              : t("alertsSettings.alerts.danger.passphraseWaiting", {
                  seconds: secondsLeft,
                })
          }
          className="h-9 w-48 font-mono text-sm"
          onChange={(e) => setPassphrase(e.target.value)}
        />
      </div>
      <Button
        type="submit"
        size="sm"
        variant="destructive"
        disabled={!gateOpen || pending}
        aria-label={t("alertsSettings.alerts.danger.confirmAria")}
      >
        {pending
          ? t("alertsSettings.alerts.danger.confirming")
          : gateOpen
            ? t("alertsSettings.alerts.danger.confirmSubmit")
            : t("alertsSettings.alerts.danger.countdown", {
                seconds: secondsLeft,
              })}
      </Button>
    </form>
  );
}
