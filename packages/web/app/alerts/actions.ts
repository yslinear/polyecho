"use server";

/**
 * Alerts / Web Push / Dangerous-action Server Actions (PRD §5.7 + §5.8).
 *
 * Tier map (PRD §5.7 操作分級):
 *  - subscribePush / unsubscribePush  -> SAFE writes (a device opt-in row).
 *  - getVapidPublicKey                -> read-only; PUBLIC key only, NEVER
 *    touches the VAPID private key (that lives in p5-core's env, not the DB).
 *  - confirmDangerousAction           -> DANGEROUS, INLINE single-step
 *    confirm. The operator decided the elaborate "create a pending request
 *    then confirm it separately" UI is unnecessary; a dangerous op now
 *    requires an INLINE repeated confirmation — an explicit type-to-confirm
 *    phrase AND the operator danger passphrase — in ONE authenticated
 *    Server Action. It does NOT weaken the real gate: the passphrase is
 *    still verified (constant-time) against the operator-provisioned
 *    `settings.danger_passphrase_hash`, fail-closed if unset (NEVER
 *    auto-provisioned). Only after BOTH the type-to-confirm match AND the
 *    passphrase verify pass does it insert ONE `pending_actions` row
 *    written DIRECTLY as status `confirmed` (audited, with params + an
 *    `inline_confirmed_at` marker), then best-effort emits the push event.
 *
 *    *** Safety equivalence: p5-core's `PendingActionExecutor` only ever
 *    consumes rows whose `status == 'confirmed'` and NEVER re-verifies the
 *    passphrase. The OLD two-step path produced a `confirmed` row only
 *    after a passphrase verify; this inline path produces a `confirmed`
 *    row only after the SAME passphrase verify (just atomically, no
 *    separate `awaiting_confirm` row). The executor's contract is
 *    therefore UNCHANGED — every row it sees is still passphrase-proven.
 *    Its defense-in-depth gates (expired-TTL / danger-passphrase-
 *    provisioned) still apply. ***
 *
 *    The Python `shared.pending_actions` create/confirm helpers remain the
 *    CLI / Python-initiated two-step contract (unchanged & still tested);
 *    only the WEB mechanism is now inline.
 *
 * The web NEVER emits events or sends pushes (core/push own that). These
 * actions only write the opt-in / confirmed rows trader-core reads.
 *
 * Drizzle import/style mirrors app/settings/actions.ts (bare upsert, no
 * redirect; islands toast + router.refresh()).
 */

import { and, eq, lte, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import {
  pushSubscriptions,
  pendingActions,
  settings,
  events,
} from "@/lib/db/schema";
import {
  VAPID_PUBLIC_KEY,
  DANGER_PASSPHRASE_HASH_KEY,
  coerceStringSetting,
} from "@/lib/settings-keys";
import { verifyPassphrase } from "@/lib/passphrase";
import {
  CONFIRM_PHRASE,
  type PendingActionType,
  type ConfirmDangerousActionInput,
  type ConfirmDangerousActionResult,
} from "./dangerous-shared";

/**
 * Frozen Phase-5 Wave-1 contract mirror (packages/shared
 * polyecho/shared/events.py — do NOT diverge).
 *
 * The Python `emit_event` writes one `events` row whose jsonb `payload`
 * column is EXACTLY `{ "data": <payload>, "push": <PushPayload.to_json()
 * or null> }`, leaves `pushed_at` NULL (the sender stamps it), then —
 * strictly AFTER the row is committed — issues
 * `SELECT pg_notify('polyecho_events', '<event_id>')` with the id as a
 * decimal string. The web is TS and cannot call the Python helper, so it
 * reproduces those semantics byte-for-byte here.
 *
 * `EVENTS_NOTIFY_CHANNEL` mirrors events.py:64 verbatim — the
 * web-push-sender LISTENs on exactly this string.
 */
const EVENTS_NOTIFY_CHANNEL = "polyecho_events" as const;

/** Mirrors `PushPayload.to_json()` — the frozen `{title,body,url,tag,data}`
 * key set the Service Worker renders (events.py:120-134). */
interface PushPayloadJson {
  title: string;
  body: string;
  url: string;
  tag: string | null;
  data: Record<string, unknown>;
}

/**
 * emitPendingDangerousActionEvent — the TS mirror of Python `emit_event`
 * for the `pending_dangerous_action` push event (PRD §5.8 待確認的危險操作).
 *
 * Best-effort, exactly like the Python intent: a failure here MUST NOT
 * fail the pending-action creation, so this never throws — it logs and
 * returns. Drizzle's postgres-js client autocommits each statement, so the
 * `events` INSERT is durably committed before the separate `pg_notify`
 * statement runs — the same "commit THEN notify" ordering guarantee the
 * Python `emit_event` provides (events.py:179-193), so the sender can
 * always SELECT the row the instant it receives the id.
 */
async function emitPendingDangerousActionEvent(args: {
  pendingActionId: string;
  actionType: PendingActionType;
  params: Record<string, unknown>;
}): Promise<void> {
  try {
    const { pendingActionId, actionType, params } = args;
    const actionLabel =
      actionType === "emergency_liquidate" ? "緊急清倉" : "手動賣出";
    const push: PushPayloadJson = {
      title: "待確認的危險操作",
      body: `有一筆「${actionLabel}」危險操作待確認，請於 5 分鐘內完成二次確認，逾時自動失效。`,
      url: "/alerts",
      tag: `pending:${pendingActionId}`,
      data: { url: "/alerts" },
    };
    // payload jsonb shape is FROZEN: { data: <payload>, push: <push or null> }
    const payload = {
      data: {
        pending_action_id: pendingActionId,
        action_type: actionType,
        params_summary: params,
      },
      push,
    };

    const [evRow] = await db
      .insert(events)
      .values({
        event_type: "pending_dangerous_action",
        severity: "warning",
        payload,
        // pushed_at omitted -> NULL (the sender stamps it after delivery).
      })
      .returning({ id: events.id });

    // NOTIFY strictly AFTER the row is committed (autocommit above). The id
    // is an integer -> injection-safe by construction; we still bind it as
    // a parameter via the tagged template, mirroring the Python
    // `pg_notify(:chan, :payload)` bound-param call (events.py:187-192).
    if (evRow?.id != null) {
      await db.execute(
        sql`select pg_notify(${EVENTS_NOTIFY_CHANNEL}, ${String(evRow.id)})`
      );
    }
  } catch (err) {
    // Best-effort: never break pending-action creation (mirrors the
    // Python best-effort intent). Log without leaking payload contents.
    console.error(
      "[alerts] pending_dangerous_action event emit failed (best-effort, ignored)",
      err
    );
  }
}

// --- Web Push -------------------------------------------------------------

export interface PushSubscriptionJSON {
  endpoint: string;
  keys: { p256dh: string; auth: string };
}

export interface SubscribePushResult {
  ok: boolean;
  message: string;
}

/**
 * getVapidPublicKey — returns ONLY the public VAPID key from the
 * `vapid_public_key` settings row (bare-string/{value} tolerant). The
 * private key is never stored in `settings` and is never returned here;
 * it lives exclusively in the p5-core push-sender process env.
 */
export async function getVapidPublicKey(): Promise<string> {
  const [row] = await db
    .select({ value: settings.value })
    .from(settings)
    .where(eq(settings.key, VAPID_PUBLIC_KEY))
    .limit(1);
  return coerceStringSetting(row?.value).trim();
}

/**
 * subscribePush — UPSERT on the UNIQUE endpoint. On conflict we refresh the
 * p256dh/auth keys + device_label and bump last_used_at. We never log the
 * keys (no console of secrets, per the brief).
 */
export async function subscribePush(
  sub: PushSubscriptionJSON,
  deviceLabel: string
): Promise<SubscribePushResult> {
  const endpoint = (sub?.endpoint ?? "").trim();
  const p256dh = (sub?.keys?.p256dh ?? "").trim();
  const auth = (sub?.keys?.auth ?? "").trim();

  if (!endpoint || !p256dh || !auth) {
    return { ok: false, message: "訂閱資料不完整，請重試。" };
  }

  const now = new Date();
  const label = deviceLabel.trim().slice(0, 120) || "未知裝置";

  await db
    .insert(pushSubscriptions)
    .values({
      endpoint,
      p256dh_key: p256dh,
      auth_key: auth,
      device_label: label,
      last_used_at: now,
    })
    .onConflictDoUpdate({
      target: pushSubscriptions.endpoint,
      set: {
        p256dh_key: p256dh,
        auth_key: auth,
        device_label: label,
        last_used_at: now,
      },
    });

  return { ok: true, message: "已開啟此裝置的推播通知。" };
}

/** unsubscribePush — delete the row for this endpoint (idempotent). */
export async function unsubscribePush(
  endpoint: string
): Promise<SubscribePushResult> {
  const e = (endpoint ?? "").trim();
  if (!e) return { ok: false, message: "缺少訂閱端點。" };
  await db.delete(pushSubscriptions).where(eq(pushSubscriptions.endpoint, e));
  return { ok: true, message: "已關閉此裝置的推播通知。" };
}

// --- Dangerous-action INLINE confirm (PRD §5.7 危险 tier) ----------------
// PendingActionType / CONFIRM_PHRASE / ConfirmDangerousAction{Input,Result}
// live in ./dangerous-shared and are imported above: a "use server" module
// may export ONLY async functions, and the client island needs the
// constant + types too.

/**
 * JSON marker written into the confirmed row's `params` recording WHEN the
 * inline confirm happened (audit trail). It is NOT a status and NOT a
 * passphrase — `core/pending.py` ignores unknown params keys; its own
 * idempotency marker is the separate `_core_processed_at`.
 */
const INLINE_CONFIRMED_AT_KEY = "_inline_confirmed_at" as const;

/**
 * confirmDangerousAction — the INLINE single-step danger-op confirm.
 *
 * Replaces the old `createPendingAction` + `confirmPendingAction` two-step.
 * One authenticated Server Action that performs the dangerous op directly
 * (by writing a `confirmed` row trader-core then executes). The gates, in
 * order (all FAIL CLOSED, nothing is written until ALL pass):
 *
 *  (a) `actionType` is one of the two known danger ops;
 *  (b) the verbatim type-to-confirm phrase exactly equals
 *      {@link CONFIRM_PHRASE} (deliberate anti-misclick second input —
 *      replaces the old 5s countdown, which only guarded the two-step UI);
 *  (c) `settings.danger_passphrase_hash` MUST be provisioned (else reject
 *      with a clear "set it via the CLI" message — NEVER auto-provision);
 *  (d) constant-time verify the submitted passphrase against that stored
 *      FROZEN canonical `pbkdf2$<iters>$<b64salt>$<b64hash>` string
 *      (rejects sub-100000-iter / tampered values). The web NEVER writes
 *      the hash;
 *  (e) only on success INSERT one `pending_actions` row written DIRECTLY
 *      as status `confirmed` (no transient `awaiting_confirm` row), with
 *      `confirm_passphrase_hash` left NULL and an `_inline_confirmed_at`
 *      audit marker in params; then best-effort emit the push event.
 *
 * Safety equivalence vs the removed two-step path: trader-core's
 * `PendingActionExecutor` only ever consumes `status == 'confirmed'` rows
 * and NEVER re-verifies the passphrase. The two-step path only reached
 * `confirmed` after a passphrase verify; this inline path only reaches
 * `confirmed` after the SAME constant-time passphrase verify — so every
 * row the executor sees is still passphrase-proven. The executor's
 * defense-in-depth (expired-TTL / danger-passphrase-provisioned) is
 * unchanged. The danger gate is preserved, only the MECHANISM is inline.
 */
export async function confirmDangerousAction(
  input: ConfirmDangerousActionInput
): Promise<ConfirmDangerousActionResult> {
  const actionType = input?.actionType;
  const params = input?.params ?? {};
  const confirmText = input?.confirmText ?? "";
  const passphrase = input?.passphrase ?? "";

  // (a) known danger op only.
  if (actionType !== "manual_sell" && actionType !== "emergency_liquidate") {
    return { ok: false, message: "未知的操作類型。" };
  }

  // (b) verbatim type-to-confirm — the anti-misclick second input.
  if (confirmText.trim() !== CONFIRM_PHRASE) {
    return {
      ok: false,
      message: `請完整輸入「${CONFIRM_PHRASE}」以確認此危險操作。`,
    };
  }

  // Passphrase presence (the real second factor).
  if (passphrase.length < 1) {
    return { ok: false, message: "請輸入危險操作通行碼。" };
  }

  // (c) read the operator-provisioned danger passphrase hash. FAIL CLOSED
  // if it has not been provisioned — never auto-provision / derive it.
  const [hashRow] = await db
    .select({ value: settings.value })
    .from(settings)
    .where(eq(settings.key, DANGER_PASSPHRASE_HASH_KEY))
    .limit(1);
  const storedHash = coerceStringSetting(hashRow?.value).trim();
  if (!storedHash) {
    return {
      ok: false,
      message:
        "危險操作密碼尚未設定，請先用 CLI `polyecho account set-danger-passphrase` 佈建後再試。",
    };
  }

  // (d) constant-time verify against the FROZEN canonical stored string.
  // Nothing has been written yet; a wrong passphrase writes NOTHING.
  const valid = await verifyPassphrase(passphrase, storedHash);
  if (!valid) {
    return { ok: false, message: "通行碼錯誤，未執行任何操作。請重試。" };
  }

  // (e) success — INSERT the row DIRECTLY as `confirmed` (trader-core
  // consumes confirmed rows; it never re-verifies the passphrase). We
  // NEVER write passphrase material (`confirm_passphrase_hash` stays NULL).
  // An `_inline_confirmed_at` audit marker records when it was confirmed.
  const auditParams: Record<string, unknown> = {
    ...params,
    [INLINE_CONFIRMED_AT_KEY]: new Date().toISOString(),
  };
  const [row] = await db
    .insert(pendingActions)
    .values({
      action_type: actionType,
      params: auditParams,
      status: "confirmed",
    })
    .returning({ id: pendingActions.id });

  // PRD §5.8: best-effort `pending_dangerous_action` push event so the
  // operator gets a record. A failure here MUST NOT fail the confirmed
  // action (it never throws), mirroring the Python `emit_event` intent.
  if (row?.id) {
    await emitPendingDangerousActionEvent({
      pendingActionId: row.id,
      actionType,
      params,
    });
  }

  return {
    ok: true,
    message: "已確認並送出此危險操作，trader-core 將執行（不再二次驗證）。",
    id: row?.id,
  };
}

/**
 * Best-effort sweep: mark any long-overdue `awaiting_confirm` rows expired
 * (idempotent). The inline web flow never creates `awaiting_confirm` rows,
 * but the Python CLI two-step contract still can, so this stays useful as
 * a janitor and is harmless when there are none.
 */
export async function expireStalePendingActions(): Promise<void> {
  await db
    .update(pendingActions)
    .set({ status: "expired" })
    .where(
      and(
        eq(pendingActions.status, "awaiting_confirm"),
        lte(pendingActions.expires_at, new Date())
      )
    );
}
