"use client";

/**
 * GlobalPauseToggle — top-bar 全域暫停/恢復 (PRD §5.8).
 *
 * P7d / R24 asymmetry (THE HIGHEST OF THE 5 INLINE-CONFIRM OPS):
 *   - PAUSE (paused=false → true): one click. Calm instrument chrome.
 *   - RESUME (paused=true → false): MUST pass the inline-confirm primitive
 *     with BOTH the verbatim CONFIRM_PHRASE AND a danger passphrase. Resume
 *     lifts the system-wide kill-switch; this is intentionally the most
 *     ceremonious web write (same gate as §5.7 danger ops).
 *
 * The button is rendered into the layout header so it must stay compact;
 * when paused, we swap the single button for the inline-confirm primitive
 * (collapsed-by-default trigger expands inline; not a modal).
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { PauseCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { InlineConfirm } from "@/components/inline-confirm";
import { setGlobalPaused } from "@/app/settings/actions";

export function GlobalPauseToggle({ paused }: { paused: boolean }) {
  const router = useRouter();
  const [pending, setPending] = React.useState(false);

  async function onPause() {
    setPending(true);
    try {
      const res = await setGlobalPaused(true);
      if (res.ok) {
        toast.success(res.message);
        router.refresh();
      } else {
        toast.error(res.message);
      }
    } catch {
      toast.error("無法更新全域暫停狀態，請檢查資料庫連線。");
    } finally {
      setPending(false);
    }
  }

  if (paused) {
    // RESUME is the highest-tier inline-confirm: both factors required.
    return (
      <InlineConfirm
        title="恢復全部跟單"
        description="這會解除全域暫停。trader-core 將於下一筆領隊交易繼續跟單。請完成二次確認後送出。"
        requirePassphrase={true}
        triggerVariant="danger"
        triggerClassName="h-8 gap-1.5 text-xs"
        successToast="已恢復：trader-core 於下一筆領隊交易繼續跟單。"
        action={async ({ confirmText, passphrase }) =>
          setGlobalPaused(false, { confirmText, passphrase })
        }
      />
    );
  }

  return (
    <Button
      variant="outline"
      size="sm"
      className="gap-1.5 text-xs"
      disabled={pending}
      aria-label="暫停全部跟單"
      aria-pressed={false}
      title="點擊全域暫停所有跟單"
      onClick={onPause}
    >
      <PauseCircle className="h-3.5 w-3.5" aria-hidden="true" />
      暫停全部
    </Button>
  );
}
