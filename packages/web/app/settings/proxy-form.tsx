"use client";

/**
 * ProxyForm — client island for the Polymarket proxy setting.
 *
 * Single text input + one primary action. Inline validation mirrors the
 * server action (http(s) only; SOCKS rejected). On success: sonner toast +
 * router.refresh() so the Server Component re-reads the persisted value.
 * No card grid, no modal — a calm single control (DESIGN.md).
 *
 * P7d / R24 asymmetry:
 *   - CLEAR (empty value): safe-direction → keeps the bare single click.
 *   - SET / CHANGE (non-empty value): risk-increasing → must pass the inline
 *     confirm (CONFIRM_PHRASE only, no passphrase) before persist. The form
 *     reveals the inline-confirm primitive inline once the user submits a
 *     valid http(s) URL.
 */

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { InlineConfirm } from "@/components/inline-confirm";
import { t } from "@/lib/i18n";
import { setPolymarketProxy } from "./actions";

interface ProxyFormProps {
  current: string;
  /** ISO string of settings.updated_at, or null when never set. */
  lastUpdated: string | null;
}

/** Client-side mirror of the server validation (for inline-on-blur). */
function validate(value: string): string | null {
  const v = value.trim();
  if (v === "") return null; // empty === direct, allowed
  if (/^socks\d?:\/\//i.test(v))
    return t("alertsSettings.settings.proxy.errSocks");
  if (!/^https?:\/\//i.test(v))
    return t("alertsSettings.settings.proxy.errScheme");
  try {
    if (!new URL(v).hostname) throw new Error("no host");
  } catch {
    return t("alertsSettings.settings.proxy.errInvalidUrl");
  }
  return null;
}

export function ProxyForm({ current, lastUpdated }: ProxyFormProps) {
  const router = useRouter();
  const [value, setValue] = useState(current);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const [pending, startTransition] = useTransition();

  const dirty = value.trim() !== current.trim();
  const trimmed = value.trim();
  const isClear = trimmed === "";

  function onClearSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setTouched(true);
    if (!isClear) return; // safety: only this form path handles the clear
    startTransition(async () => {
      try {
        const res = await setPolymarketProxy("");
        if (res.ok) {
          toast.success(res.message);
          setError(null);
          router.refresh();
        } else {
          setError(res.message);
          toast.error(res.message);
        }
      } catch {
        toast.error(t("alertsSettings.settings.proxy.errSaveFailed"));
      }
    });
  }

  return (
    <form onSubmit={onClearSubmit} className="space-y-3" noValidate>
      <div className="space-y-1.5">
        <label
          htmlFor="polymarket_proxy_url"
          className="text-sm font-medium leading-none"
        >
          {t("alertsSettings.settings.proxy.urlLabel")}
        </label>
        <Input
          id="polymarket_proxy_url"
          name="polymarket_proxy_url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder={t("alertsSettings.settings.proxy.urlPlaceholder")}
          value={value}
          aria-invalid={touched && error ? true : undefined}
          aria-describedby="proxy-help proxy-error"
          className="max-w-md font-mono text-sm"
          onChange={(e) => {
            setValue(e.target.value);
            if (touched) setError(validate(e.target.value));
          }}
          onBlur={() => {
            setTouched(true);
            setError(validate(value));
          }}
        />
        {touched && error ? (
          <p id="proxy-error" className="text-sm text-loss" role="alert">
            {error}
          </p>
        ) : (
          <p id="proxy-help" className="max-w-prose text-xs text-muted-foreground">
            {t("alertsSettings.settings.proxy.formHelp")}
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        {isClear ? (
          // Safe-direction: clearing the proxy stays single-click.
          <Button type="submit" disabled={pending || !dirty}>
            {pending
              ? t("alertsSettings.settings.proxy.saving")
              : t("alertsSettings.settings.proxy.save")}
          </Button>
        ) : (
          // Risk-increasing: setting a NEW proxy must inline-confirm.
          <InlineConfirm
            title={t("alertsSettings.settings.proxy.save")}
            description="這會改變 trader-core 對 Polymarket 的對外連線位址。請完成二次確認後送出（將於下一次 Polymarket 請求生效，無需重啟）。"
            requirePassphrase={false}
            triggerVariant="default"
            triggerClassName="h-9 gap-1.5"
            successToast="Proxy saved. Takes effect on the next Polymarket call."
            action={async ({ confirmText }) => {
              const localErr = validate(value);
              if (localErr) {
                setError(localErr);
                setTouched(true);
                return { ok: false, message: localErr };
              }
              const res = await setPolymarketProxy({
                value,
                confirmText,
              });
              if (!res.ok) setError(res.message);
              return res;
            }}
          />
        )}
        <span className="text-xs text-muted-foreground tabular-nums">
          {value.trim() === ""
            ? t("alertsSettings.settings.proxy.currentDirect")
            : lastUpdated
              ? t("alertsSettings.settings.proxy.lastSaved", {
                  at: lastUpdated.slice(0, 16).replace("T", " "),
                })
              : t("alertsSettings.settings.proxy.notSaved")}
        </span>
      </div>
    </form>
  );
}
