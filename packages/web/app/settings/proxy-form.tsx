"use client";

/**
 * ProxyForm — client island for the Polymarket proxy setting.
 *
 * Single text input + one primary action. Inline validation mirrors the
 * server action (http(s) only; SOCKS rejected). On success: sonner toast +
 * router.refresh() so the Server Component re-reads the persisted value.
 * No card grid, no modal — a calm single control (DESIGN.md).
 */

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  if (/^socks\d?:\/\//i.test(v)) return "SOCKS not supported; use http(s)";
  if (!/^https?:\/\//i.test(v))
    return "Proxy URL must start with http:// or https://";
  try {
    if (!new URL(v).hostname) throw new Error("no host");
  } catch {
    return "Enter a valid http(s) URL, e.g. http://127.0.0.1:8080";
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

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setTouched(true);
    const localErr = validate(value);
    setError(localErr);
    if (localErr) return;

    startTransition(async () => {
      try {
        const res = await setPolymarketProxy(value);
        if (res.ok) {
          toast.success(res.message);
          setError(null);
          router.refresh();
        } else {
          setError(res.message);
          toast.error(res.message);
        }
      } catch {
        toast.error("Could not save the proxy. Check the database connection.");
      }
    });
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3" noValidate>
      <div className="space-y-1.5">
        <label
          htmlFor="polymarket_proxy_url"
          className="text-sm font-medium leading-none"
        >
          Proxy URL
        </label>
        <Input
          id="polymarket_proxy_url"
          name="polymarket_proxy_url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="http://127.0.0.1:8080  (blank = direct)"
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
            Needed where Polymarket is geo-blocked, PRD §8.1. http(s) only.
            Takes effect on the next Polymarket call, no restart.
          </p>
        )}
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" disabled={pending || !dirty}>
          {pending ? "Saving…" : "Save proxy"}
        </Button>
        <span className="text-xs text-muted-foreground tabular-nums">
          {value.trim() === ""
            ? "Currently: direct connection"
            : lastUpdated
              ? `Last saved ${lastUpdated.slice(0, 16).replace("T", " ")} UTC`
              : "Not yet saved"}
        </span>
      </div>
    </form>
  );
}
