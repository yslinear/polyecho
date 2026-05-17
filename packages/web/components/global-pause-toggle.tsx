"use client";

/**
 * GlobalPauseToggle — top-bar one-click 全域暫停/恢復 (PRD §5.8).
 *
 * Calm instrument chrome: a single outline button reflecting current state
 * read by the layout Server Component. On click it calls the
 * `setGlobalPaused` SAFE Server Action, toasts, and router.refresh()es so
 * the shell re-reads. Consistent with the per-leader Pause/Resume island.
 *
 * Not a modal-first flow: pausing is reversible and safe, so a confirm
 * dialog would be friction. (Dangerous ops live on /alerts behind the
 * passphrase gate — that is a different tier.)
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { PauseCircle, PlayCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { setGlobalPaused } from "@/app/settings/actions";

export function GlobalPauseToggle({ paused }: { paused: boolean }) {
  const router = useRouter();
  const [pending, setPending] = React.useState(false);

  async function onToggle() {
    setPending(true);
    try {
      const res = await setGlobalPaused(!paused);
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

  const Icon = paused ? PlayCircle : PauseCircle;
  return (
    <Button
      variant={paused ? "default" : "outline"}
      size="sm"
      className="gap-1.5 text-xs"
      disabled={pending}
      aria-label={paused ? "恢復全部跟單" : "暫停全部跟單"}
      aria-pressed={paused}
      title={paused ? "目前已全域暫停，點擊恢復" : "點擊全域暫停所有跟單"}
      onClick={onToggle}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {paused ? "已暫停" : "暫停全部"}
    </Button>
  );
}
