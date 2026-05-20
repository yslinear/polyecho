/**
 * Shared server-side gate for the P7d INLINE-confirm Server Actions
 * (5 RED ops, R24 design intent). Imported by the 5 Server Actions in:
 *   - app/settings/actions.ts:setGlobalPaused(false) ← REQUIRES passphrase
 *   - app/settings/actions.ts:setRiskMode("normal")
 *   - app/settings/actions.ts:setPolymarketProxy(new value)
 *   - app/discovery/actions.ts:updateLeaderStatus("following")
 *   - app/leaders/actions.ts:setFollowStatus("active")
 *
 * Gate order (FAIL CLOSED, nothing is written until ALL pass):
 *   (a) verbatim type-to-confirm phrase == CONFIRM_PHRASE
 *   (b) if requirePassphrase: presence + constant-time verify against the
 *       operator-provisioned `settings.danger_passphrase_hash`. NEVER auto-
 *       provision; absence is a clear "set it via the CLI" reject.
 *
 * Mirrors `app/alerts/actions.ts:confirmDangerousAction` ordering byte-for-
 * byte (PRD §5.7) so the two confirm flows stay in lockstep. Lives outside
 * the "use server" modules because "use server" only allows async functions
 * to be exported.
 */

import { db } from "@/lib/db";
import { loadDangerPassphraseHash, verifyPassphrase } from "@/lib/passphrase";
import { CONFIRM_PHRASE } from "@/app/alerts/dangerous-shared";

export interface InlineConfirmGateInput {
  /** Verbatim type-to-confirm phrase (must equal CONFIRM_PHRASE). */
  confirmText?: string | null;
  /** Danger passphrase — required IFF requirePassphrase=true. */
  passphrase?: string | null;
  /** When true, ALSO verify the danger passphrase (highest-tier ops). */
  requirePassphrase?: boolean;
}

export interface InlineConfirmGateOk {
  ok: true;
}
export interface InlineConfirmGateFail {
  ok: false;
  message: string;
}
export type InlineConfirmGateResult = InlineConfirmGateOk | InlineConfirmGateFail;

/**
 * Run the inline-confirm gate. Returns `{ok:true}` only when ALL required
 * checks pass; otherwise `{ok:false, message}` so the Server Action can
 * early-return WITHOUT writing.
 */
export async function checkInlineConfirm(
  input: InlineConfirmGateInput
): Promise<InlineConfirmGateResult> {
  const confirmText = (input.confirmText ?? "").trim();
  if (confirmText !== CONFIRM_PHRASE) {
    return {
      ok: false,
      message: `請完整輸入「${CONFIRM_PHRASE}」以確認此操作。`,
    };
  }

  if (!input.requirePassphrase) return { ok: true };

  const passphrase = input.passphrase ?? "";
  if (passphrase.length < 1) {
    return { ok: false, message: "請輸入危險操作通行碼。" };
  }

  const storedHash = await loadDangerPassphraseHash();
  if (!storedHash) {
    return {
      ok: false,
      message:
        "危險操作密碼尚未設定，請先用 CLI `polyecho account set-danger-passphrase` 佈建後再試。",
    };
  }

  const valid = await verifyPassphrase(passphrase, storedHash);
  if (!valid) {
    return { ok: false, message: "通行碼錯誤，未執行任何操作。請重試。" };
  }
  return { ok: true };
}

/**
 * Best-effort audit emit for an inline-confirmed operation. Writes ONE
 * `events` row whose payload records the op_type + arbitrary key/value
 * context. NEVER throws (a failed audit must NOT fail the mutation it
 * follows, mirroring the alerts.ts emit_event intent).
 */
export async function emitInlineConfirmAudit(args: {
  opType: string;
  context: Record<string, unknown>;
  requirePassphrase: boolean;
}): Promise<void> {
  try {
    const { events } = await import("@/lib/db/schema");
    await db.insert(events).values({
      event_type: `inline_confirm.${args.opType}`,
      severity: args.requirePassphrase ? "warning" : "info",
      payload: {
        data: {
          op_type: args.opType,
          require_passphrase: args.requirePassphrase,
          inline_confirmed_at: new Date().toISOString(),
          ...args.context,
        },
        push: null,
      },
    });
  } catch (err) {
    // Best-effort: log without leaking payload contents.
    console.error(
      "[inline-confirm] audit emit failed (best-effort, ignored)",
      err
    );
  }
}
