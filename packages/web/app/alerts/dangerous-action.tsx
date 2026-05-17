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
    const t = target.trim();
    if (!t) {
      toast.error("請輸入目標（市場 / 部位 / 帳戶識別）。");
      return;
    }
    const params: Record<string, unknown> = { target: t };
    if (actionType === "manual_sell") {
      const s = size.trim();
      if (!s) {
        toast.error("手動賣出需要指定數量。");
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
      toast.error("建立待確認操作失敗，請檢查資料庫連線。");
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
        建立危險操作請求
      </Button>
    );
  }

  return (
    <form
      onSubmit={onCreate}
      className="space-y-4 rounded-lg border border-dashed p-4"
      aria-label="建立危險操作請求"
    >
      <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
        <AlertTriangle
          className="mt-0.5 h-3.5 w-3.5 shrink-0 text-loss"
          aria-hidden="true"
        />
        <span>
          這是危險操作（PRD §5.7）。建立後需經 5 秒倒數與通行碼二次確認，
          trader-core 只執行已確認的請求。
        </span>
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label
            htmlFor="da-type"
            className="text-sm font-medium leading-none"
          >
            操作類型
          </label>
          <Select
            value={actionType}
            onValueChange={(v) => setActionType(v as PendingActionType)}
          >
            <SelectTrigger id="da-type" className="text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="manual_sell">手動賣出</SelectItem>
              <SelectItem value="emergency_liquidate">緊急清倉</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="da-target"
            className="text-sm font-medium leading-none"
          >
            目標
          </label>
          <Input
            id="da-target"
            value={target}
            spellCheck={false}
            autoComplete="off"
            placeholder="市場 / 部位 / 帳戶 ID"
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
              賣出數量
            </label>
            <Input
              id="da-size"
              inputMode="decimal"
              value={size}
              autoComplete="off"
              placeholder="例如 100"
              className="font-mono text-sm tabular-nums"
              onChange={(e) => setSize(e.target.value)}
            />
          </div>
        ) : null}
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" size="sm" disabled={pending}>
          {pending ? "建立中…" : "建立待確認請求"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={pending}
          onClick={() => setOpen(false)}
        >
          取消
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
      toast.error("請輸入確認通行碼。");
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
      toast.error("確認失敗，請檢查資料庫連線。");
    } finally {
      setPending(false);
    }
  }

  if (expired) {
    return (
      <p className="text-xs text-muted-foreground">此操作已逾時，請重新建立。</p>
    );
  }

  return (
    <form onSubmit={onConfirm} className="flex flex-wrap items-end gap-3">
      <div className="space-y-1.5">
        <label
          htmlFor={`pp-${id}`}
          className="text-xs font-medium leading-none text-muted-foreground"
        >
          確認通行碼
        </label>
        <Input
          id={`pp-${id}`}
          type="password"
          value={passphrase}
          autoComplete="off"
          disabled={!gateOpen || pending}
          placeholder={
            gateOpen ? "輸入通行碼" : `請等待 ${secondsLeft} 秒…`
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
        aria-label="確認執行此危險操作"
      >
        {pending
          ? "確認中…"
          : gateOpen
            ? "確認執行"
            : `倒數 ${secondsLeft} 秒`}
      </Button>
    </form>
  );
}
