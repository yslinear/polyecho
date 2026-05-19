/**
 * Shared (NON-"use server") constants + types for the §5.7 inline
 * dangerous-action confirm.
 *
 * A Next.js "use server" module (app/alerts/actions.ts) may export ONLY
 * async functions — a runtime `const` or a `type`/`interface` export there
 * fails the build ("Only async functions are allowed to be exported in a
 * 'use server' file"). The constant + types that BOTH the Server Action
 * and its client island (app/alerts/dangerous-action.tsx) need therefore
 * live here, in a plain module, and are imported by both sides.
 */

export type PendingActionType = "manual_sell" | "emergency_liquidate";

/**
 * The fixed phrase the operator must type VERBATIM (a deliberate,
 * un-mis-clickable second factor alongside the danger passphrase). Kept
 * here (not i18n) so it is the exact, locale-independent literal the
 * operator types and the server compares byte-for-byte.
 */
export const CONFIRM_PHRASE = "確認執行" as const;

export interface ConfirmDangerousActionInput {
  actionType: PendingActionType;
  /** Opaque execution params (jsonb) — e.g. { target, size }. */
  params: Record<string, unknown>;
  /** The verbatim type-to-confirm phrase (must equal {@link CONFIRM_PHRASE}). */
  confirmText: string;
  /** The operator danger passphrase (verified, never stored). */
  passphrase: string;
}

export interface ConfirmDangerousActionResult {
  ok: boolean;
  message: string;
  /** The id of the directly-`confirmed` pending_actions row, on success. */
  id?: string;
}
