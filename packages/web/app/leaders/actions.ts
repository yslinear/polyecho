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
 *  - DANGEROUS ops (manual sell, emergency liquidation) — operator
 *    confirmation happens via the web ``dangerous-action`` flow.
 *  - HIGH-DANGER ops (withdrawals, key edits) — CLI ONLY.
 *
 * Drizzle import/style mirrors app/discovery/actions.ts. No redirect — the
 * client island toasts + router.refresh()es so the table re-reads.
 */

import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { followRelations } from "@/lib/db/schema";
import {
  checkInlineConfirm,
  emitInlineConfirmAudit,
} from "@/lib/inline-confirm-gate";

export type FollowStatus = "active" | "paused";

export interface SetFollowStatusResult {
  id: string;
  status: FollowStatus;
}

/**
 * setFollowStatus — pause ('paused') or resume ('active') one follow
 * relation. Stamps paused_at when pausing; clears it when resuming so the
 * row's lifecycle stays honest for trader-core's reconcile cursor.
 *
 * P7d / R24 asymmetry:
 *   - status="paused" is safe-direction → single-click, NO gate.
 *   - status="active" (RESUME a paused leader) is risk-increasing and must
 *     pass the inline-confirm gate (CONFIRM_PHRASE only, no passphrase)
 *     BEFORE the row is rewritten.
 *
 * The third arg carries the optional inline-confirm payload so the pause
 * call site keeps the bare two-arg shape (back-compat).
 */
export async function setFollowStatus(
  id: string,
  status: FollowStatus,
  confirm?: { confirmText?: string | null }
): Promise<SetFollowStatusResult> {
  if (status === "active") {
    const gate = await checkInlineConfirm({
      confirmText: confirm?.confirmText ?? null,
    });
    if (!gate.ok) {
      throw new Error(gate.message);
    }
  }

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

  if (status === "active") {
    await emitInlineConfirmAudit({
      opType: "set_follow_status_active",
      context: { id },
      requirePassphrase: false,
    });
  }

  return { id, status };
}
