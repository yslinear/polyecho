"use server";

/**
 * Alerts / Web Push / Dangerous-action Server Actions (PRD §5.7 + §5.8).
 *
 * Tier map (PRD §5.7 操作分級):
 *  - subscribePush / unsubscribePush  -> SAFE writes (a device opt-in row).
 *  - getVapidPublicKey                -> read-only; PUBLIC key only, NEVER
 *    touches the VAPID private key (that lives in p5-core's env, not the DB).
 *  - createPendingAction              -> DANGEROUS request (status
 *    `awaiting_confirm`, 300s TTL). Creating the row does NOT execute it
 *    and writes NO passphrase material (`confirm_passphrase_hash` stays
 *    NULL — it is unused now).
 *  - confirmPendingAction             -> DANGEROUS confirm. SERVER-SIDE 5s
 *    min-age gate (NOT the client countdown) THEN passphrase verified
 *    against the operator-provisioned `settings.danger_passphrase_hash`.
 *    Web OWNS verification: it reproduces the FROZEN canonical
 *    `pbkdf2$<iters>$<b64salt>$<b64hash>` PBKDF2-HMAC-SHA256 format
 *    byte-for-byte and only on success flips status -> `confirmed`. If the
 *    settings hash is absent it FAILS CLOSED (never auto-provisions).
 *    *** p5-core only EXECUTES `confirmed` rows and does NOT re-verify the
 *    passphrase. *** The Python `shared.pending_actions` helpers are for
 *    CLI/Python-initiated flows only.
 *
 * The web NEVER emits events or sends pushes (core/push own that). These
 * actions only write the opt-in / request / confirm rows trader-core reads.
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

// --- Dangerous-action two-step confirm (PRD §5.7 危险 tier) ---------------

export type PendingActionType = "manual_sell" | "emergency_liquidate";

/** Default TTL — a pending row auto-expires 300s after creation. */
const PENDING_TTL_SECONDS = 300;

/**
 * SERVER-SIDE minimum age before a pending row may be confirmed. Mirrors
 * the frozen Python gate constant `MIN_CONFIRM_AGE_SECONDS = 5`. Enforced
 * here against the DB `created_at` — we do NOT trust the client countdown.
 */
const MIN_CONFIRM_AGE_SECONDS = 5;

export interface CreatePendingResult {
  ok: boolean;
  message: string;
  id?: string;
}

/**
 * createPendingAction — insert an `awaiting_confirm` row with a 300s TTL.
 * Creating the row does NOT execute anything; confirmation is required.
 * It writes NO passphrase material: `confirm_passphrase_hash` is left out
 * of the insert so it stays NULL (it is unused — the danger passphrase is
 * the operator-provisioned `settings.danger_passphrase_hash`).
 */
export async function createPendingAction(
  actionType: PendingActionType,
  params: Record<string, unknown>
): Promise<CreatePendingResult> {
  if (actionType !== "manual_sell" && actionType !== "emergency_liquidate") {
    return { ok: false, message: "未知的操作類型。" };
  }
  const expires_at = new Date(Date.now() + PENDING_TTL_SECONDS * 1000);
  const [row] = await db
    .insert(pendingActions)
    .values({
      action_type: actionType,
      params,
      status: "awaiting_confirm",
      expires_at,
    })
    .returning({ id: pendingActions.id });

  // PRD §5.8 待確認的危險操作: emit the frozen `pending_dangerous_action`
  // push event so the web-push-sender notifies the operator. Best-effort —
  // a failure here must NOT fail the pending-action creation (it never
  // throws), mirroring the Python `emit_event` best-effort intent.
  if (row?.id) {
    await emitPendingDangerousActionEvent({
      pendingActionId: row.id,
      actionType,
      params,
    });
  }

  return {
    ok: true,
    message: "已建立待確認的危險操作，請於 5 分鐘內完成二次確認。",
    id: row?.id,
  };
}

export interface ConfirmPendingResult {
  ok: boolean;
  message: string;
}

/**
 * confirmPendingAction — gate then verify, constant-time, then flip the row
 * to `confirmed`. The gates, in order (all FAIL CLOSED):
 *
 *  (a) row must exist, be `awaiting_confirm`, and not past `expires_at`;
 *  (b) SERVER-SIDE min-age: reject if `now - created_at < 5s` (we do NOT
 *      trust the client countdown — the DB `created_at` is authoritative);
 *  (c) read `settings.danger_passphrase_hash`. If absent -> reject with a
 *      clear "set it via the CLI" message. NEVER auto-provision;
 *  (d) verify the submitted passphrase against that stored FROZEN canonical
 *      `pbkdf2$<iters>$<b64salt>$<b64hash>` string (constant-time; rejects
 *      sub-100000-iter / tampered strings). The web NEVER writes the hash;
 *  (e) only on success flip status -> `confirmed`. Wrong passphrase -> the
 *      row is NOT consumed (TTL handles expiry). p5-core executes
 *      `confirmed` rows without re-verifying.
 */
export async function confirmPendingAction(
  id: string,
  passphrase: string
): Promise<ConfirmPendingResult> {
  const pid = (id ?? "").trim();
  const pass = passphrase ?? "";
  if (!pid) return { ok: false, message: "缺少操作識別碼。" };
  if (pass.length < 1) return { ok: false, message: "請輸入確認通行碼。" };

  // (a) load the row.
  const [row] = await db
    .select({
      id: pendingActions.id,
      status: pendingActions.status,
      expires_at: pendingActions.expires_at,
      created_at: pendingActions.created_at,
    })
    .from(pendingActions)
    .where(eq(pendingActions.id, pid))
    .limit(1);

  if (!row) return { ok: false, message: "找不到此操作，可能已逾時清除。" };
  if (row.status !== "awaiting_confirm") {
    return {
      ok: false,
      message: `此操作已是「${row.status}」狀態，無法重複確認。`,
    };
  }
  if (row.expires_at && new Date(row.expires_at).getTime() <= Date.now()) {
    return { ok: false, message: "此操作已逾時，請重新建立。" };
  }

  // (b) SERVER-SIDE minimum-age gate — authoritative against created_at, NOT
  // the client countdown. A missing/unparseable created_at fails closed.
  const createdMs = row.created_at
    ? new Date(row.created_at).getTime()
    : Number.NaN;
  if (!Number.isFinite(createdMs)) {
    return { ok: false, message: "此操作建立時間異常，請重新建立。" };
  }
  const ageMs = Date.now() - createdMs;
  if (ageMs < MIN_CONFIRM_AGE_SECONDS * 1000) {
    const waitS = Math.ceil(
      (MIN_CONFIRM_AGE_SECONDS * 1000 - ageMs) / 1000
    );
    return {
      ok: false,
      message: `為避免誤觸，請於建立後至少 ${MIN_CONFIRM_AGE_SECONDS} 秒再確認（尚需 ${waitS} 秒）。`,
    };
  }

  // (c) read the operator-provisioned danger passphrase hash. FAIL CLOSED if
  // it has not been provisioned — never auto-provision / derive it here.
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
  const valid = await verifyPassphrase(pass, storedHash);
  if (!valid) {
    // Do NOT consume the row — let the TTL expire it.
    return { ok: false, message: "通行碼錯誤。請重試（操作仍待確認）。" };
  }

  // (e) success — flip to confirmed. We NEVER write the passphrase hash.
  await db
    .update(pendingActions)
    .set({ status: "confirmed" })
    .where(
      and(
        eq(pendingActions.id, pid),
        eq(pendingActions.status, "awaiting_confirm")
      )
    );

  return {
    ok: true,
    message: "已確認。trader-core 將執行此操作（不再二次驗證通行碼）。",
  };
}

/** rejectPendingAction — operator cancels before confirming (safe write). */
export async function rejectPendingAction(
  id: string
): Promise<ConfirmPendingResult> {
  const pid = (id ?? "").trim();
  if (!pid) return { ok: false, message: "缺少操作識別碼。" };
  await db
    .update(pendingActions)
    .set({ status: "rejected" })
    .where(
      and(
        eq(pendingActions.id, pid),
        eq(pendingActions.status, "awaiting_confirm")
      )
    );
  return { ok: true, message: "已取消此待確認操作。" };
}

/** Best-effort sweep: mark long-overdue awaiting rows expired (idempotent). */
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
