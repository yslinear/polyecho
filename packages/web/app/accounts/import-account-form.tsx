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
 * FIELD ORDER (informed, not a blind upfront pick): 帳戶名稱(選填) → EOA →
 * (auto read-only balance of the EOA; for signature_type 0 this IS the
 * account's value) → 簽章類型 (deliberately near the END) → Funder (only
 * type≠0; also probed) → 私鑰 → 加密密語 → 危險操作密碼 → submit.
 *
 * The balance probe is a PURE PUBLIC READ (`probeBalance`): best-effort,
 * debounced once per settled address, fail-soft, and NEVER blocks submit.
 * Full multi-signature-type detection (incl. derived funders) is the CLI
 * `polyecho account detect <EOA>` — this page only shows the balance of an
 * address it actually has (the EOA, or an explicit operator funder).
 *
 * The client-side validation and the `importAccount` submit call (and its
 * server-side gates) are UNCHANGED — this is purely input-order + an
 * additive read-only balance display.
 *
 * On success: sonner toast + router.refresh() so the read-only Server
 * Component re-reads the (now `ready`) row.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { KeyRound, ShieldAlert, Loader2 } from "lucide-react";

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
import { EVM_ADDRESS_RE, PRIVATE_KEY_RE, formatUsd } from "@/lib/format";
import { importAccount } from "./actions";
import { probeTotalBalance } from "./balance";

/** Debounce (ms) so the probe fires once per SETTLED address, not per key. */
const PROBE_DEBOUNCE_MS = 600;

type ProbeState =
  | { status: "idle" }
  | { status: "probing" }
  | {
      status: "done";
      ok: boolean;
      positionsUsd: number | null;
      idleUsdc: number | null;
      totalUsd: number | null;
    };

/**
 * useAddressBalance — fires the read-only `probeTotalBalance` once the
 * address has settled to a valid EVM shape (debounced). Stale results
 * are dropped. `enabled` lets the caller suppress the funder probe for
 * signature_type 0. Each call returns the data-api positions value (a),
 * the on-chain idle USDC.e (b), and their composed total.
 */
function useAddressBalance(address: string, enabled: boolean): ProbeState {
  const [state, setState] = React.useState<ProbeState>({ status: "idle" });

  React.useEffect(() => {
    const trimmed = address.trim();
    if (!enabled || !EVM_ADDRESS_RE.test(trimmed)) {
      setState({ status: "idle" });
      return;
    }

    let cancelled = false;
    const handle = setTimeout(() => {
      setState({ status: "probing" });
      probeTotalBalance(trimmed)
        .then((res) => {
          if (cancelled) return;
          setState({
            status: "done",
            ok: res.ok,
            positionsUsd: res.positionsUsd,
            idleUsdc: res.idleUsdc,
            totalUsd: res.totalUsd,
          });
        })
        .catch(() => {
          // probeTotalBalance never throws, but stay defensive: fail soft.
          if (!cancelled) {
            setState({
              status: "done",
              ok: false,
              positionsUsd: null,
              idleUsdc: null,
              totalUsd: null,
            });
          }
        });
    }, PROBE_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [address, enabled]);

  return state;
}

/** Render one labelled balance row (label · value-or-N/A). */
function BalanceRow({
  label,
  value,
}: {
  label: string;
  value: number | null;
}) {
  return (
    <p className="text-xs text-muted-foreground">
      <span className="font-medium text-foreground">{label}：</span>
      <span className="ml-1">
        {value === null ? t("tables.accounts.import.balanceNa") : formatUsd(value)}
      </span>
    </p>
  );
}

/**
 * Inline, read-only composite balance display: three labelled lines
 * (部位 / 閒置 / 合計). Never blocks submit; fail-soft copy. Each half
 * renders independently — a successful half is shown even if the other
 * half failed.
 */
function BalanceLine({
  state,
  note,
}: {
  state: ProbeState;
  note: string;
}) {
  if (state.status === "idle") return null;

  if (state.status === "probing") {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
        <span>{t("tables.accounts.import.balanceProbing")}</span>
      </p>
    );
  }

  // status === "done"
  if (
    !state.ok &&
    state.positionsUsd === null &&
    state.idleUsdc === null
  ) {
    return (
      <p className="text-xs text-muted-foreground">
        {t("tables.accounts.import.balanceFailed")}
      </p>
    );
  }

  return (
    <div className="space-y-0.5">
      <BalanceRow
        label={t("tables.accounts.import.balancePositionsLabel")}
        value={state.positionsUsd}
      />
      <BalanceRow
        label={t("tables.accounts.import.balanceIdleLabel")}
        value={state.idleUsdc}
      />
      <BalanceRow
        label={t("tables.accounts.import.balanceTotalLabel")}
        value={state.totalUsd}
      />
      <p className="text-xs text-muted-foreground">{note}</p>
    </div>
  );
}

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

  // Progressive-disclosure gates — derived synchronously from state. The
  // identity section (name / EOA / Funder / sigType) is always editable;
  // the secrets section (private key, enc pass, danger pass) is visible
  // but visually muted + interaction-blocked until the identity inputs
  // are valid, so the operator can SEE the next steps without filling
  // them out-of-order. The Server Action re-validates authoritatively;
  // these gates are purely UX.
  const eoaValid = EVM_ADDRESS_RE.test(eoa.trim());
  const funderValid = isType0 || EVM_ADDRESS_RE.test(funder.trim());
  const identityValid = eoaValid && funderValid;
  const secretsLocked = !identityValid;
  const privateKeyValid = PRIVATE_KEY_RE.test(privateKey.trim());
  const fullValid =
    identityValid &&
    privateKeyValid &&
    encPass.length > 0 &&
    dangerPass.length > 0;

  // Read-only, best-effort balance of the addresses we actually HAVE. The
  // EOA is always probed. Funder is probed whenever the operator types a
  // valid EVM there, REGARDLESS of sigType — that way the funder balance
  // is visible BEFORE the operator picks sigType, so "informed choice"
  // (sigTypeHint copy) is actually achievable. type 0 still ignores
  // funder at submit time (server-side normalisation).
  const eoaBalance = useAddressBalance(eoa, true);
  const funderBalance = useAddressBalance(funder, true);

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

      {/* Identity section — always editable, informed-choice order.
          Funder is now BEFORE sigType so the operator can see its balance
          BEFORE picking a signature type (the sigTypeHint promise). */}
      <div className="grid gap-4 sm:grid-cols-2">
        {/* 1. 帳戶名稱（選填） */}
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

        {/* 2. EOA 位址 + 即時餘額 */}
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
          <BalanceLine
            state={eoaBalance}
            note={t("tables.accounts.import.balanceEoaNote")}
          />
        </div>

        {/* 3. Funder + 即時餘額 — always enabled. The operator may pre-fill
              it to see its on-chain balance BEFORE committing to a
              signature type; type 0 ignores funder at submit time. */}
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
            placeholder={t("tables.accounts.import.funderPlaceholder")}
            className="font-mono text-sm"
            onChange={(e) => setFunder(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {isType0
              ? t("tables.accounts.import.funderHintType0")
              : t("tables.accounts.import.funderHintOther")}
          </p>
          <BalanceLine
            state={funderBalance}
            note={t("tables.accounts.import.balanceFunderNote")}
          />
        </div>

        {/* 4. 簽章類型 — informed choice AFTER both balances are visible */}
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
          <p className="text-xs text-muted-foreground">
            {t("tables.accounts.import.sigTypeHint")}
          </p>
        </div>
      </div>

      {/* Secrets section — visible (so the operator anticipates next steps)
          but locked until identity is valid (EOA + funder-when-not-type-0).
          opacity-50 is the visual cue; `disabled` on inputs makes them
          inert for a11y / keyboard. Submit button also disabled below
          until ALL fields are valid. */}
      <div
        aria-hidden={secretsLocked || undefined}
        className={
          "grid gap-4 sm:grid-cols-2 transition-opacity " +
          (secretsLocked ? "opacity-50" : "")
        }
      >
        {/* 5. 私鑰 */}
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
            disabled={secretsLocked}
            placeholder={t("tables.accounts.import.privateKeyPlaceholder")}
            className="font-mono text-sm"
            onChange={(e) => setPrivateKey(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {t("tables.accounts.import.privateKeyHint")}
          </p>
        </div>

        {/* 6. 私鑰加密密語 */}
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
            spellCheck={false}
            disabled={secretsLocked}
            placeholder={t("tables.accounts.import.encPassPlaceholder")}
            className="text-sm"
            onChange={(e) => setEncPass(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {t("tables.accounts.import.encPassHint")}
          </p>
        </div>

        {/* 7. 危險操作確認密碼 */}
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
            spellCheck={false}
            disabled={secretsLocked}
            placeholder={t("tables.accounts.import.dangerPassPlaceholder")}
            className="max-w-md text-sm"
            onChange={(e) => setDangerPass(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {t("tables.accounts.import.dangerPassHint")}
          </p>
        </div>
      </div>

      {/* Honest scope note: this page only balances the address it HAS. */}
      <p className="text-xs text-muted-foreground">
        {t("tables.accounts.import.balanceCliHint")}
      </p>

      <div className="flex items-center gap-3">
        <Button
          type="submit"
          size="sm"
          variant="destructive"
          disabled={pending || !fullValid}
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
