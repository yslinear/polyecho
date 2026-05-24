"use client";

/**
 * AddWalletDialog — manual smart-wallet add (operator UX).
 *
 * A plain submit Dialog (NOT an InlineConfirm — InlineConfirm is the
 * danger-passphrase primitive reserved for money-path / live operations,
 * and manually adding an OBSERVATION candidate is explicitly not one).
 *
 * Flow:
 *   1. Operator clicks "新增錢包" in the page header.
 *   2. Dialog opens with: EVM address input + optional note + submit/cancel.
 *   3. Inline shape validation: a non-matching address disables submit and
 *      shows the inline error after the field is touched.
 *   4. On submit, `addCandidateLeader` runs. Three outcomes:
 *        - ok=true  → success toast, close dialog, router.refresh (the
 *                     Server Action also revalidatePath("/discovery")).
 *        - already_tracked → in-dialog hint with the existing
 *                            lifecycle_state. Dialog stays open so the
 *                            operator can dismiss / try another address.
 *        - error / invalid_address → generic error toast.
 *
 * Header placement: mounted in app/discovery/page.tsx next to the existing
 * "新增 Backtest" link (the only other page-header CTA).
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { EVM_ADDRESS_RE } from "@/lib/format";
import { t } from "@/lib/i18n";
import { addCandidateLeader } from "./actions";

export function AddWalletDialog() {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [address, setAddress] = React.useState("");
  const [note, setNote] = React.useState("");
  const [touched, setTouched] = React.useState(false);
  const [pending, startTransition] = React.useTransition();
  const [duplicateState, setDuplicateState] = React.useState<string | null>(
    null,
  );

  // Reset every controlled bit when the Dialog is dismissed so the next open
  // starts clean (otherwise a previous error / value would leak across opens).
  const handleOpenChange = React.useCallback((next: boolean) => {
    setOpen(next);
    if (!next) {
      setAddress("");
      setNote("");
      setTouched(false);
      setDuplicateState(null);
    }
  }, []);

  const trimmed = address.trim();
  const isShapeValid =
    trimmed.length > 0 &&
    trimmed.length <= 64 &&
    EVM_ADDRESS_RE.test(trimmed);
  const showError = touched && !isShapeValid;

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setTouched(true);
    if (!isShapeValid || pending) return;
    setDuplicateState(null);
    startTransition(async () => {
      try {
        const res = await addCandidateLeader({ address: trimmed, note });
        if (res.ok) {
          toast.success(t("discovery.list.addWallet.successToast"));
          handleOpenChange(false);
          router.refresh();
          return;
        }
        if (res.reason === "already_tracked") {
          setDuplicateState(res.lifecycle_state);
          return;
        }
        // invalid_address (client-side guard usually catches it first) +
        // generic error → same toast tier.
        toast.error(t("discovery.list.addWallet.genericError"));
      } catch {
        toast.error(t("discovery.list.addWallet.genericError"));
      }
    });
  };

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
      >
        {t("discovery.list.addWallet.button")}
      </Button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("discovery.list.addWallet.title")}</DialogTitle>
            <DialogDescription>
              {t("discovery.list.addWallet.description")}
            </DialogDescription>
          </DialogHeader>

          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="add-wallet-address">
                {t("discovery.list.addWallet.addressLabel")}
              </Label>
              <Input
                id="add-wallet-address"
                value={address}
                onChange={(e) => {
                  setAddress(e.target.value);
                  if (duplicateState) setDuplicateState(null);
                }}
                onBlur={() => setTouched(true)}
                placeholder={t("discovery.list.addWallet.addressPlaceholder")}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={showError || undefined}
                aria-describedby={
                  showError ? "add-wallet-address-error" : undefined
                }
                disabled={pending}
              />
              {showError ? (
                <p
                  id="add-wallet-address-error"
                  role="alert"
                  className="text-sm text-loss"
                >
                  {t("discovery.list.addWallet.addressError")}
                </p>
              ) : null}
            </div>

            <div className="space-y-2">
              <Label htmlFor="add-wallet-note">
                {t("discovery.list.addWallet.noteLabel")}
              </Label>
              <Input
                id="add-wallet-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder={t("discovery.list.addWallet.notePlaceholder")}
                autoComplete="off"
                maxLength={500}
                disabled={pending}
              />
            </div>

            {duplicateState ? (
              <p role="status" className="text-sm text-muted-foreground">
                {t("discovery.list.addWallet.duplicate", {
                  state: duplicateState,
                })}
              </p>
            ) : null}

            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                onClick={() => handleOpenChange(false)}
                disabled={pending}
              >
                {t("discovery.list.addWallet.cancel")}
              </Button>
              <Button type="submit" disabled={!isShapeValid || pending}>
                {pending
                  ? t("discovery.list.addWallet.submitting")
                  : t("discovery.list.addWallet.submit")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
