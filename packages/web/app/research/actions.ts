"use server";

/**
 * Research Server Actions — the C1 paper-trading ladder's ONLY money-path.
 *
 * `promoteToLiveFollow` is the SINGLE explicit operator action that turns a
 * `trial_ready` candidate leader into a real follow. The C1 ladder state
 * machine (C1-P3, packages/paper) NEVER inserts a `follow_relations` row and
 * NEVER sets a follow active — `trial_ready` is advisory only. This action
 * is the deliberate, human-gated bridge from the paper ladder to real money.
 *
 * The money-safety gate (PRD §5.7 危险 tier + roadmap C1 "Money-safety
 * boundary") is TWO independent locks:
 *
 *   1. Danger-passphrase confirm — the same inline-confirm gate the other
 *      RED Server Actions use (`checkInlineConfirm` with
 *      requirePassphrase=true): a verbatim type-to-confirm phrase AND a
 *      constant-time verify of the operator-provisioned
 *      `settings.danger_passphrase_hash`. Fail-closed; nothing is written
 *      until BOTH pass.
 *   2. The inserted `follow_relations` row is created status='paused' (NOT
 *      'active'). Even a successful promotion does NOT start copy-trading —
 *      the operator must still explicitly Resume it on /leaders, which is
 *      itself danger-confirmed (`setFollowStatus("active")`). Double gate.
 *
 * Server-side rejection: a candidate that is not `lifecycle_state =
 * 'trial_ready'`, or is `locked`, is rejected BEFORE any write. The client
 * island only ever offers the control on a trial_ready row, but a Server
 * Action is a public RPC — the authoritative check is here.
 *
 * This is the FIRST code in the repo that INSERTs a `follow_relations` row.
 * It links a sane default account + copy_template (the earliest of each);
 * the operator can re-point them on /leaders before resuming.
 *
 * Drizzle import/style mirrors app/leaders/actions.ts.
 */

import { asc, eq } from "drizzle-orm";

import { db } from "@/lib/db";
import {
  accounts,
  candidateLeaders,
  copyTemplates,
  followRelations,
} from "@/lib/db/schema";
import {
  checkInlineConfirm,
  emitInlineConfirmAudit,
} from "@/lib/inline-confirm-gate";
import type { PromoteToLiveFollowResult } from "./research-shared";

/**
 * promoteToLiveFollow — promote a `trial_ready` candidate leader to a real
 * (but PAUSED) follow relation. Returns a `{ ok, message }` envelope so the
 * caller (the InlineConfirm primitive) can toast without throwing.
 *
 * Gate order (FAIL CLOSED — nothing is written until ALL pass):
 *   (a) danger-passphrase inline-confirm gate (phrase + constant-time hash);
 *   (b) the candidate exists;
 *   (c) it is NOT `locked` (operator override skips it from the ladder);
 *   (d) its `lifecycle_state` is exactly `trial_ready`;
 *   (e) it has no existing follow_relations row (idempotent — no dupes);
 *   (f) a default account + copy_template both exist to link.
 * Only then INSERT one follow_relations row at status='paused'.
 */
export async function promoteToLiveFollow(input: {
  leaderAddress: string;
  confirmText?: string;
  passphrase?: string;
}): Promise<PromoteToLiveFollowResult> {
  const leaderAddress = (input?.leaderAddress ?? "").trim();

  // (a) danger-passphrase gate — the FIRST money-safety lock. requirePass-
  // phrase=true: the verbatim phrase AND a constant-time verify of the
  // operator-provisioned danger_passphrase_hash. Nothing written until pass.
  const gate = await checkInlineConfirm({
    confirmText: input?.confirmText ?? null,
    passphrase: input?.passphrase ?? null,
    requirePassphrase: true,
  });
  if (!gate.ok) {
    return { ok: false, message: gate.message };
  }

  // (b) the candidate must exist. Read its ladder state + lock so the
  // rejection messages below are specific.
  const [candidate] = await db
    .select({
      wallet_address: candidateLeaders.wallet_address,
      lifecycle_state: candidateLeaders.lifecycle_state,
      locked: candidateLeaders.locked,
    })
    .from(candidateLeaders)
    .where(eq(candidateLeaders.wallet_address, leaderAddress))
    .limit(1);

  if (!candidate) {
    return {
      ok: false,
      message: `找不到候選 Leader ${leaderAddress}。`,
    };
  }

  // (c) a locked candidate is skipped by the ladder entirely — never
  // promotable.
  if (candidate.locked) {
    return {
      ok: false,
      message: "此候選 Leader 已被鎖定，無法升級。",
    };
  }

  // (d) ONLY trial_ready is promotable — the authoritative server-side
  // rejection of any other ladder state.
  if (candidate.lifecycle_state !== "trial_ready") {
    return {
      ok: false,
      message: "僅能升級 trial_ready 狀態的候選 Leader。",
    };
  }

  // (e) idempotency: never insert a second follow_relations row for a
  // leader that is already followed.
  const [existing] = await db
    .select({ id: followRelations.id })
    .from(followRelations)
    .where(eq(followRelations.leader_address, leaderAddress))
    .limit(1);
  if (existing) {
    return {
      ok: false,
      message: "此 Leader 已存在跟單關係，未重複建立。",
    };
  }

  // (f) link a sane default account + copy_template. follow_relations
  // requires both NOT-NULL FK columns; the earliest of each is the
  // deterministic default and the operator can re-point them on /leaders
  // before resuming.
  const [account] = await db
    .select({ id: accounts.id })
    .from(accounts)
    .orderBy(asc(accounts.created_at))
    .limit(1);
  if (!account) {
    return {
      ok: false,
      message: "尚無可用帳戶，請先於 /accounts 匯入一個帳戶。",
    };
  }

  const [template] = await db
    .select({ id: copyTemplates.id })
    .from(copyTemplates)
    .orderBy(asc(copyTemplates.name))
    .limit(1);
  if (!template) {
    return {
      ok: false,
      message: "尚無 copy template，請先於 CLI 建立一個跟單模板。",
    };
  }

  // All gates passed — INSERT the follow_relations row at status='paused'.
  // The SECOND money-safety lock: even this successful promotion does NOT
  // start copy-trading. The operator must still explicitly Resume it on
  // /leaders (itself danger-confirmed). paused_at is stamped so trader-
  // core's reconcile loop honours the pause from the first cycle.
  const [row] = await db
    .insert(followRelations)
    .values({
      account_id: account.id,
      leader_address: leaderAddress,
      template_id: template.id,
      status: "paused",
      paused_at: new Date(),
    })
    .returning({ id: followRelations.id });

  // Best-effort audit: a follow promotion is a money-path event; record it
  // (never throws — a failed audit must not fail the promotion).
  await emitInlineConfirmAudit({
    opType: "promote_to_live_follow",
    context: {
      leader_address: leaderAddress,
      follow_relation_id: row?.id ?? null,
      account_id: account.id,
      template_id: template.id,
      created_status: "paused",
    },
    requirePassphrase: true,
  });

  return {
    ok: true,
    message:
      "已升級為暫停中的真實跟單，請至 /leaders 確認後恢復。",
    followRelationId: row?.id,
  };
}

// Re-export the guard so a hypothetical CLI/test path can reject without
// duplicating the rule. The trial_ready/locked rejection above is the
// canonical implementation; this keeps the predicate addressable.
export async function isPromotable(
  lifecycleState: string,
  locked: boolean
): Promise<boolean> {
  return lifecycleState === "trial_ready" && !locked;
}
