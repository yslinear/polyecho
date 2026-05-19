"use client";

/**
 * ImportAccountForm — client island for the operator-authorised web
 * private-key import (recorded override of the CLI-only model).
 *
 * SECURITY:
 *  - The private key, the encryption passphrase, and the danger passphrase
 *    are `type="password"`, `autoComplete="off"`, and are NEVER rendered
 *    back, echoed in a toast, or placed in the DOM after submit. On success
 *    every secret field is cleared.
 *  - All validation messages describe shape only and never include the
 *    entered value.
 *  - It is an inline disclosure (DESIGN.md bans modal-first), under a
 *    clearly-labelled, explicitly-dangerous section — mirroring
 *    app/alerts/dangerous-action.tsx.
 *
 * On success: sonner toast + router.refresh() so the read-only Server
 * Component re-reads the (now `ready`) row.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { KeyRound, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { t } from "@/lib/i18n";
import { EVM_ADDRESS_RE, PRIVATE_KEY_RE } from "@/lib/format";
import { importAccount } from "./actions";

export function ImportAccountForm() {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState("");
  const [eoa, setEoa] = React.useState("");
  const [sigType, setSigType] = React.useState("0");
  const [funder, setFunder] = React.useState("");
  const [privateKey, setPrivateKey] = React.useState("");
  const [encPass, setEncPass] = React.useState("");
  const [dangerPass, setDangerPass] = React.useState("");
  const [pending, setPending] = React.useState(false);

  const isType0 = sigType === "0";

  function clearSecrets() {
    setPrivateKey("");
    setEncPass("");
    setDangerPass("");
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();

    // Client-side mirror of the server validation (the Server Action
    // re-validates authoritatively — this is just early UX feedback).
    if (!EVM_ADDRESS_RE.test(eoa.trim())) {
      toast.error(t("tables.accounts.import.errEoa"));
      return;
    }
    if (!PRIVATE_KEY_RE.test(privateKey.trim())) {
      toast.error(t("tables.accounts.import.errPrivateKey"));
      return;
    }
    if (!isType0 && !EVM_ADDRESS_RE.test(funder.trim())) {
      toast.error(t("tables.accounts.import.errFunder"));
      return;
    }
    if (encPass.length < 1) {
      toast.error(t("tables.accounts.import.errEncPass"));
      return;
    }
    if (dangerPass.length < 1) {
      toast.error(t("tables.accounts.import.errDangerPass"));
      return;
    }

    setPending(true);
    try {
      const res = await importAccount({
        eoaAddress: eoa.trim(),
        privateKey: privateKey.trim(),
        signatureType: Number(sigType),
        funder: funder.trim(),
        name: name.trim(),
        dangerPassphrase: dangerPass,
        encryptionPassphrase: encPass,
      });
      if (res.ok) {
        toast.success(res.message);
        // Wipe ALL fields (especially secrets) and collapse the form.
        setName("");
        setEoa("");
        setSigType("0");
        setFunder("");
        clearSecrets();
        setOpen(false);
        router.refresh();
      } else {
        toast.error(res.message);
        // Keep non-secret fields for correction; always wipe secrets.
        clearSecrets();
      }
    } catch {
      toast.error(t("tables.accounts.import.errFailed"));
      clearSecrets();
    } finally {
      setPending(false);
    }
  }

  if (!open) {
    return (
      <Button
        variant="outline"
        size="sm"
        className="text-xs"
        onClick={() => setOpen(true)}
      >
        <KeyRound className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
        {t("tables.accounts.import.openButton")}
      </Button>
    );
  }

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-4 rounded-lg border border-dashed p-4"
      aria-label={t("tables.accounts.import.heading")}
      autoComplete="off"
      noValidate
    >
      <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
        <ShieldAlert
          className="mt-0.5 h-3.5 w-3.5 shrink-0 text-loss"
          aria-hidden="true"
        />
        <span>{t("tables.accounts.import.intro")}</span>
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label htmlFor="ia-name" className="text-sm font-medium leading-none">
            {t("tables.accounts.import.nameLabel")}
          </label>
          <Input
            id="ia-name"
            value={name}
            autoComplete="off"
            spellCheck={false}
            placeholder={t("tables.accounts.import.namePlaceholder")}
            className="text-sm"
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="ia-sig" className="text-sm font-medium leading-none">
            {t("tables.accounts.import.sigTypeLabel")}
          </label>
          <Select value={sigType} onValueChange={setSigType}>
            <SelectTrigger id="ia-sig" className="text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="0">
                {t("tables.accounts.import.sigType0")}
              </SelectItem>
              <SelectItem value="1">
                {t("tables.accounts.import.sigType1")}
              </SelectItem>
              <SelectItem value="2">
                {t("tables.accounts.import.sigType2")}
              </SelectItem>
              <SelectItem value="3">
                {t("tables.accounts.import.sigType3")}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="ia-eoa" className="text-sm font-medium leading-none">
            {t("tables.accounts.import.eoaLabel")}
          </label>
          <Input
            id="ia-eoa"
            value={eoa}
            autoComplete="off"
            spellCheck={false}
            placeholder={t("tables.accounts.import.eoaPlaceholder")}
            className="font-mono text-sm"
            onChange={(e) => setEoa(e.target.value)}
          />
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="ia-funder"
            className="text-sm font-medium leading-none"
          >
            {t("tables.accounts.import.funderLabel")}
          </label>
          <Input
            id="ia-funder"
            value={funder}
            autoComplete="off"
            spellCheck={false}
            disabled={isType0}
            placeholder={t("tables.accounts.import.funderPlaceholder")}
            className="font-mono text-sm"
            onChange={(e) => setFunder(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {isType0
              ? t("tables.accounts.import.funderHintType0")
              : t("tables.accounts.import.funderHintOther")}
          </p>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="ia-pk" className="text-sm font-medium leading-none">
            {t("tables.accounts.import.privateKeyLabel")}
          </label>
          <Input
            id="ia-pk"
            type="password"
            value={privateKey}
            autoComplete="off"
            spellCheck={false}
            placeholder={t("tables.accounts.import.privateKeyPlaceholder")}
            className="font-mono text-sm"
            onChange={(e) => setPrivateKey(e.target.value)}
          />
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor="ia-encpass"
            className="text-sm font-medium leading-none"
          >
            {t("tables.accounts.import.encPassLabel")}
          </label>
          <Input
            id="ia-encpass"
            type="password"
            value={encPass}
            autoComplete="off"
            placeholder={t("tables.accounts.import.encPassPlaceholder")}
            className="text-sm"
            onChange={(e) => setEncPass(e.target.value)}
          />
        </div>

        <div className="space-y-1.5 sm:col-span-2">
          <label
            htmlFor="ia-dangerpass"
            className="text-sm font-medium leading-none"
          >
            {t("tables.accounts.import.dangerPassLabel")}
          </label>
          <Input
            id="ia-dangerpass"
            type="password"
            value={dangerPass}
            autoComplete="off"
            placeholder={t("tables.accounts.import.dangerPassPlaceholder")}
            className="max-w-md text-sm"
            onChange={(e) => setDangerPass(e.target.value)}
          />
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Button
          type="submit"
          size="sm"
          variant="destructive"
          disabled={pending}
        >
          {pending
            ? t("tables.accounts.import.submitting")
            : t("tables.accounts.import.submit")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={pending}
          onClick={() => {
            clearSecrets();
            setOpen(false);
          }}
        >
          {t("tables.accounts.import.cancel")}
        </Button>
      </div>
    </form>
  );
}
