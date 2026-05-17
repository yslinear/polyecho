"use server";

/**
 * Alerts / Web Push / Dangerous-action Server Actions (PRD §5.7 + §5.8).
 *
 * Tier map (PRD §5.7 操作分級):
 *  - subscribePush / unsubscribePush  -> SAFE writes (a device opt-in row).
 *  - getVapidPublicKey                -> read-only; PUBLIC key only, NEVER
 *    touches the VAPID private key (that lives in p5-core's env, not the DB).
 *  - createPendingAction              -> DANGEROUS request (status
 *    `awaiting_confirm`, 300s TTL). Creating the row does NOT execute it.
 *  - confirmPendingAction             -> DANGEROUS confirm. 5s UI countdown
 *    gate (client) THEN passphrase. Web OWNS verification: it hashes with
 *    PBKDF2-HMAC-SHA256 and only on success flips status -> `confirmed`.
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

import { and, eq, lte } from "drizzle-orm";

import { db } from "@/lib/db";
import { pushSubscriptions, pendingActions, settings } from "@/lib/db/schema";
import { VAPID_PUBLIC_KEY, coerceStringSetting } from "@/lib/settings-keys";
import { hashPassphrase, verifyPassphrase } from "@/lib/passphrase";

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

const PENDING_TTL_SECONDS = 300;

export interface CreatePendingResult {
  ok: boolean;
  message: string;
  id?: string;
}

/**
 * createPendingAction — insert an `awaiting_confirm` row with a 300s TTL.
 * Creating the row does NOT execute anything; confirmation is required.
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
 * confirmPendingAction — verify the passphrase (constant-time) and flip the
 * row to `confirmed`. Wrong passphrase -> error, row is NOT consumed (TTL
 * handles expiry). Expired -> rejected. Web owns verification; p5-core
 * executes `confirmed` rows without re-verifying.
 */
export async function confirmPendingAction(
  id: string,
  passphrase: string
): Promise<ConfirmPendingResult> {
  const pid = (id ?? "").trim();
  const pass = passphrase ?? "";
  if (!pid) return { ok: false, message: "缺少操作識別碼。" };
  if (pass.length < 1) return { ok: false, message: "請輸入確認通行碼。" };

  const [row] = await db
    .select({
      id: pendingActions.id,
      status: pendingActions.status,
      expires_at: pendingActions.expires_at,
      confirm_passphrase_hash: pendingActions.confirm_passphrase_hash,
    })
    .from(pendingActions)
    .where(eq(pendingActions.id, pid))
    .limit(1);

  if (!row) return { ok: false, message: "找不到此操作，可能已逾時清除。" };
  if (row.status !== "awaiting_confirm") {
    return { ok: false, message: `此操作已是「${row.status}」狀態，無法重複確認。` };
  }
  if (row.expires_at && new Date(row.expires_at).getTime() <= Date.now()) {
    return { ok: false, message: "此操作已逾時，請重新建立。" };
  }

  // First confirm sets the per-row hash; subsequent attempts verify against it.
  let storedHash = row.confirm_passphrase_hash;
  if (!storedHash) {
    storedHash = await hashPassphrase(pass);
    await db
      .update(pendingActions)
      .set({ confirm_passphrase_hash: storedHash })
      .where(
        and(
          eq(pendingActions.id, pid),
          eq(pendingActions.status, "awaiting_confirm")
        )
      );
  }

  const valid = await verifyPassphrase(pass, storedHash);
  if (!valid) {
    // Do NOT consume the row — let the TTL expire it.
    return { ok: false, message: "通行碼錯誤。請重試（操作仍待確認）。" };
  }

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
