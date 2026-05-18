"use server";

/**
 * Leaders Server Actions — PRD §5.7 "安全寫入" (sanctioned write) tier.
 *
 * setFollowStatus: pause / resume one follow_relation. This is the ONLY
 * write the /leaders page performs and it is explicitly a SAFE write per
 * PRD §5.7 ("暫停/恢復 ... Web UI Server Action 寫 settings 表"). It flips
 * follow_relations.status between 'active' and 'paused' and stamps
 * paused_at so trader-core's reconcile loop honours the pause.
 *
 * Out of scope by design (PRD §5.7 操作分級):
 *  - DANGEROUS ops (manual sell, emergency liquidation) — CLI + countdown.
 *  - HIGH-DANGER ops (withdrawals, key edits, enabling live trading) —
 *    CLI ONLY. The web NEVER offers a live-trading enable/disable control;
 *    the binding switch is the host env POLYECHO_LIVE_TRADING read by
 *    trader-core. This module deliberately exposes no such action.
 *
 * Drizzle import/style mirrors app/discovery/actions.ts. No redirect — the
 * client island toasts + router.refresh()es so the table re-reads.
 */

import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { followRelations } from "@/lib/db/schema";

export type FollowStatus = "active" | "paused";

export interface SetFollowStatusResult {
  id: string;
  status: FollowStatus;
}

/**
 * setFollowStatus — pause ('paused') or resume ('active') one follow
 * relation. Stamps paused_at when pausing; clears it when resuming so the
 * row's lifecycle stays honest for trader-core's reconcile cursor.
 */
export async function setFollowStatus(
  id: string,
  status: FollowStatus
): Promise<SetFollowStatusResult> {
  // rev-web I-1: server-side ownership/existence check. Drizzle's
  // .returning() yields the rows the UPDATE actually matched (an empty
  // array when the WHERE matched nothing). Without this an arbitrary or
  // stale id silently "succeeds" with a 0-row UPDATE and the client toasts
  // a false positive. Throw a clear error instead so the caller's catch
  // surfaces the real failure.
  const updated = await db
    .update(followRelations)
    .set({
      status,
      paused_at: status === "paused" ? new Date() : null,
    })
    .where(eq(followRelations.id, id))
    .returning({ id: followRelations.id });

  if (updated.length === 0) {
    throw new Error(`找不到跟單關係 ${id}，狀態未更新`);
  }

  return { id, status };
}
