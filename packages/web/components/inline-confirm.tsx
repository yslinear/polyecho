"use client";

/**
 * InlineConfirm — shared inline two-input confirm primitive (P7d / R24).
 *
 * The same SHAPE as `app/alerts/dangerous-action.tsx` but generalised so the
 * 5 risk-increasing Server Actions in P7d (resume-global / promote-leader /
 * resume-leader / leave-conservative / set-new-proxy) can all reuse one
 * collapsed-button → dashed-bordered confirm form WITHOUT a modal
 * (DESIGN.md bans modal-first; calm instrument chrome).
 *
 * Tier map (mirrors the matching server gate ordering):
 *   - confirmText is ALWAYS required (verbatim `CONFIRM_PHRASE` from
 *     `app/alerts/dangerous-shared.ts` — same byte-for-byte literal the
 *     server compares). This is the anti-misclick second input.
 *   - passphrase is required ONLY when `requirePassphrase = true` (the
 *     resume-global-kill-switch case — the highest of the 5 RED ops).
 *
 * The primitive is purely a UX gate: it BLOCKS the caller's `action` from
 * being fired client-side until the gate passes, and forwards the typed
 * inputs as the LAST two named args. The Server Action MUST still re-verify
 * everything constant-time and fail-closed; this island is convenience, not
 * security.
 *
 * Why not modify `dangerous-action.tsx` directly: that file is the §5.7
 * highest-danger-tier flow (manual_sell / emergency_liquidate) with a
 * type+target+size compound form and a directly-`confirmed` `pending_actions`
 * insert. The 5 new ops are simpler (no params), so we reuse the inline
 * confirm SHAPE without coupling to that file's specific contract.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { t } from "@/lib/i18n";
import { CONFIRM_PHRASE } from "@/app/alerts/dangerous-shared";

/**
 * The Server Action signature this primitive supports: a function taking the
 * type-to-confirm phrase + (optional) danger passphrase as its LAST named
 * args, returning a `{ ok, message }` envelope. The caller binds any other
 * args through a thin wrapper so the primitive stays operation-agnostic.
 */
export type InlineConfirmAction = (input: {
  confirmText: string;
  passphrase?: string;
}) => Promise<{ ok: boolean; message: string }>;

export interface InlineConfirmProps {
  /** Bound Server Action (caller curries any operation-specific args). */
  action: InlineConfirmAction;
  /** Short zh-TW label of the operation ("恢復全部跟單" / "改用正常模式" / …). */
  title: string;
  /** Optional zh-TW explanation rendered above the inputs. Falls back to the generic intro. */
  description?: string;
  /** When true, also requires + forwards a danger passphrase (highest tier only). */
  requirePassphrase?: boolean;
  /** Sonner toast text on a successful submit (caller picks the zh-TW). */
  successToast?: string;
  /**
   * Variant of the trigger button. "danger" uses the destructive accent for
   * the highest-tier resume-kill-switch; "default" is the outline neutral
   * used by the calmer promote / resume / mode / proxy direction.
   */
  triggerVariant?: "default" | "danger";
  /** Optional className appended to the trigger button (e.g. width control). */
  triggerClassName?: string;
  /** Fired AFTER the action's `{ok:true}` envelope, BEFORE router.refresh(). */
  onConfirmed?: () => void;
}

export function InlineConfirm({
  action,
  title,
  description,
  requirePassphrase = false,
  successToast,
  triggerVariant = "default",
  triggerClassName,
  onConfirmed,
}: InlineConfirmProps) {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [confirmText, setConfirmText] = React.useState("");
  const [passphrase, setPassphrase] = React.useState("");
  const [pending, startTransition] = React.useTransition();

  function reset() {
    setConfirmText("");
    setPassphrase("");
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (confirmText.trim() !== CONFIRM_PHRASE) {
      toast.error(
        t("inlineConfirm.errConfirmPhrase", { phrase: CONFIRM_PHRASE })
      );
      return;
    }
    if (requirePassphrase && passphrase.length < 1) {
      toast.error(t("inlineConfirm.errPassphrase"));
      return;
    }
    startTransition(async () => {
      try {
        const res = await action({
          confirmText,
          passphrase: requirePassphrase ? passphrase : undefined,
        });
        if (res.ok) {
          toast.success(successToast ?? res.message);
          onConfirmed?.();
          reset();
          setOpen(false);
          router.refresh();
        } else {
          toast.error(res.message);
          // Wrong gate -> server wrote nothing; clear only the secrets so
          // the operator can re-enter without rebuilding everything.
          setConfirmText("");
          setPassphrase("");
        }
      } catch {
        toast.error(t("inlineConfirm.errSubmitFailed"));
      }
    });
  }

  if (!open) {
    return (
      <Button
        type="button"
        variant={triggerVariant === "danger" ? "destructive" : "outline"}
        size="sm"
        className={triggerClassName ?? "h-8 gap-1.5 text-xs"}
        onClick={() => setOpen(true)}
        aria-label={t("inlineConfirm.openAria", { title })}
      >
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
        {t("inlineConfirm.openButton", { title })}
      </Button>
    );
  }

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-3 rounded-lg border border-dashed border-loss/50 bg-loss/5 p-3"
      aria-label={t("inlineConfirm.formAria", { title })}
    >
      <p className="flex items-start gap-1.5 text-xs font-medium text-loss">
        <AlertTriangle
          className="mt-0.5 h-3.5 w-3.5 shrink-0"
          aria-hidden="true"
        />
        <span>{t("inlineConfirm.sectionTitle", { title })}</span>
      </p>
      <p className="text-xs text-muted-foreground">
        {description ?? t("inlineConfirm.intro")}
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label
            htmlFor="ic-confirm"
            className="text-sm font-medium leading-none"
          >
            {t("inlineConfirm.confirmPhraseLabel", { phrase: CONFIRM_PHRASE })}
          </label>
          <Input
            id="ic-confirm"
            value={confirmText}
            autoComplete="off"
            spellCheck={false}
            placeholder={t("inlineConfirm.confirmPhrasePlaceholder")}
            className="h-9 font-mono text-sm"
            onChange={(e) => setConfirmText(e.target.value)}
          />
        </div>
        {requirePassphrase ? (
          <div className="space-y-1.5">
            <label
              htmlFor="ic-pass"
              className="text-sm font-medium leading-none"
            >
              {t("inlineConfirm.passphraseLabel")}
            </label>
            <Input
              id="ic-pass"
              type="password"
              value={passphrase}
              autoComplete="off"
              spellCheck={false}
              placeholder={t("inlineConfirm.passphrasePlaceholder")}
              className="h-9 font-mono text-sm"
              onChange={(e) => setPassphrase(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {t("inlineConfirm.passphraseHint")}
            </p>
          </div>
        ) : null}
      </div>

      <div className="flex items-center gap-2">
        <Button
          type="submit"
          size="sm"
          variant={requirePassphrase ? "destructive" : "default"}
          disabled={
            pending ||
            confirmText.trim() !== CONFIRM_PHRASE ||
            (requirePassphrase && passphrase.length < 1)
          }
          title={t("inlineConfirm.submitAria", { title })}
        >
          {pending
            ? t("inlineConfirm.submitting")
            : t("inlineConfirm.submitButton")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={pending}
          onClick={() => {
            reset();
            setOpen(false);
          }}
        >
          {t("inlineConfirm.cancelButton")}
        </Button>
      </div>
    </form>
  );
}
