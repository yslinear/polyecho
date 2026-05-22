"use client";

/**
 * FunderPicker — client island for /accounts import form (P3).
 *
 * GOAL: cut "which of my 4 wallets is the real one?" out of the import
 * flow. Given a typed EOA, this component:
 *   1. Calls Server Action `deriveAllFunders(eoa)` (proxies to internal
 *      `detect` container that runs the FROZEN Python derivations).
 *   2. For each of the 4 candidates with a non-null address, calls
 *      `probeTotalBalance(addr)` in parallel to fetch positions + idle.
 *   3. Renders 4 button rows; clicking pre-fills sigType + funder in the
 *      parent form. After pick, collapses to a single read-only line
 *      with a 重選 button.
 *
 * SECURITY MODEL: still no client-side derivations. The web tier proxies
 * to the trusted Python source. Addresses shown are RENDERED ONLY — the
 * operator's submit through `importAccount` is still authoritative.
 *
 * FAIL-SOFT:
 *   - `deriveAllFunders` returns `{ok:false}` → hide rows, show a one
 *     line note. Manual Funder input below stays usable.
 *   - Individual balance probe fails → row stays selectable, balance
 *     line shows the failed copy.
 *   - All 4 candidates probe to zero → show an actionable empty note
 *     telling the operator to deposit USDC first.
 *
 * DEBOUNCE: same 600ms pattern as `useAddressBalance` — fire once per
 * settled EVM address. Re-entry on EOA change cancels in-flight work via
 * stale flags + useEffect cleanup. There is no spinner anywhere; shadcn
 * `<Skeleton>` per design.md primitives ban.
 *
 * NO em-dashes anywhere in copy (DESIGN.md hard ban).
 */

import * as React from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { t } from "@/lib/i18n";
import type { I18nKey } from "@/lib/i18n";
import { EVM_ADDRESS_RE, formatUsd, shortAddress } from "@/lib/format";
import { deriveAllFunders, type FunderCandidate } from "./actions";
import { probeTotalBalance } from "./balance";

/** Debounce (ms): same as useAddressBalance — settle-once-per-edit. */
const PICKER_DEBOUNCE_MS = 600;

type BalanceState = "idle" | "probing" | "done" | "failed";

/** Internal row state: candidate + its probed balance (kept separate so
 *  re-renders don't lose the probed numbers when other rows update). */
interface Row {
  sigType: 0 | 1 | 2 | 3;
  mode: FunderCandidate["mode"];
  address: string | null;
  derived: boolean;
  error?: string;
  balanceState: BalanceState;
  positions: number | null;
  idle: number | null;
}

/** Local i18n key for the 4 sig-type modes (NOT exported — purely UI). */
function modeLabelKey(mode: FunderCandidate["mode"]): I18nKey {
  switch (mode) {
    case "eoa":
      return "tables.accounts.import.picker.modeEoa";
    case "magic":
      return "tables.accounts.import.picker.modeMagic";
    case "safe":
      return "tables.accounts.import.picker.modeSafe";
    case "poly_1271":
      return "tables.accounts.import.picker.modePoly1271";
  }
}

/** A row "has balance" when at least one of (positions, idle) is > 0. */
function hasBalance(r: Row): boolean {
  return (
    r.balanceState === "done" &&
    ((r.positions !== null && r.positions > 0) ||
      (r.idle !== null && r.idle > 0))
  );
}

export interface FunderPickerProps {
  eoa: string;
  onPick: (c: FunderCandidate) => void;
}

export function FunderPicker({ eoa, onPick }: FunderPickerProps) {
  const trimmed = eoa.trim();
  const validEoa = EVM_ADDRESS_RE.test(trimmed);

  const [rows, setRows] = React.useState<Row[] | null>(null);
  const [unavailable, setUnavailable] = React.useState<string | null>(null);
  const [picked, setPicked] = React.useState<Row | null>(null);

  /** Reset picked state when the EOA changes (operator edits address). */
  React.useEffect(() => {
    setPicked(null);
  }, [trimmed]);

  /** Detect + probe, debounced once per settled EOA. */
  React.useEffect(() => {
    if (!validEoa) {
      setRows(null);
      setUnavailable(null);
      return;
    }

    let cancelled = false;
    const handle = setTimeout(async () => {
      // Phase 1: derive the 4 candidates from `detect`.
      const detect = await deriveAllFunders(trimmed);
      if (cancelled) return;

      if (!detect.ok) {
        setRows(null);
        setUnavailable(detect.reason);
        return;
      }

      setUnavailable(null);

      // Phase 2: seed all rows as "probing" so the skeleton renders for
      // each candidate immediately, then fire probes in parallel.
      const seeded: Row[] = detect.candidates.map((c) => ({
        sigType: c.sigType,
        mode: c.mode,
        address: c.address,
        derived: c.derived,
        ...(c.error ? { error: c.error } : {}),
        balanceState: c.address === null ? "done" : "probing",
        positions: null,
        idle: null,
      }));
      setRows(seeded);

      await Promise.all(
        detect.candidates.map(async (c, i) => {
          if (c.address === null) return;
          try {
            const res = await probeTotalBalance(c.address);
            if (cancelled) return;
            setRows((prev) => {
              if (prev === null) return prev;
              const next = prev.slice();
              next[i] = {
                ...next[i],
                balanceState: res.ok ? "done" : "failed",
                positions: res.positionsUsd,
                idle: res.idleUsdc,
              };
              return next;
            });
          } catch {
            if (cancelled) return;
            setRows((prev) => {
              if (prev === null) return prev;
              const next = prev.slice();
              next[i] = { ...next[i], balanceState: "failed" };
              return next;
            });
          }
        }),
      );
    }, PICKER_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [trimmed, validEoa]);

  // Picker is HIDDEN until the EOA is valid (R22 progressive disclosure).
  if (!validEoa) return null;

  // Service-unavailable state: small note, no rows. Manual funder input
  // stays usable below.
  if (unavailable !== null) {
    return (
      <section className="space-y-2">
        <p className="text-xs text-muted-foreground">
          {t("tables.accounts.import.picker.serviceUnavailable")}
        </p>
      </section>
    );
  }

  // Post-pick collapsed view.
  if (picked !== null) {
    return (
      <section className="space-y-2" aria-live="polite">
        <p className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">
            {t("tables.accounts.import.picker.selectedPrefix")}
          </span>
          <span className="font-medium">{t(modeLabelKey(picked.mode))}</span>
          <span className="font-mono text-muted-foreground">
            {picked.address ? shortAddress(picked.address) : t("tables.accounts.import.picker.noAddress")}
          </span>
          <button
            type="button"
            onClick={() => setPicked(null)}
            className="ml-1 rounded px-1.5 py-0.5 text-xs text-foreground/80 underline-offset-2
                       hover:text-foreground hover:underline
                       focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {t("tables.accounts.import.picker.reselect")}
          </button>
        </p>
      </section>
    );
  }

  // Still waiting for detect to resolve.
  if (rows === null) {
    return (
      <section className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h3 className="text-sm font-semibold">
            {t("tables.accounts.import.picker.heading")}
          </h3>
          <span className="text-xs text-muted-foreground">
            {t("tables.accounts.import.picker.subheading")}
          </span>
        </div>
        <ul className="divide-y divide-border rounded-md border" role="list">
          {[0, 1, 2, 3].map((i) => (
            <li key={i} className="px-3 py-3 min-h-[56px]">
              <Skeleton className="h-4 w-40" />
            </li>
          ))}
        </ul>
      </section>
    );
  }

  // All 4 candidates have finished probing AND none has a positive balance.
  const allDone = rows.every(
    (r) => r.balanceState === "done" || r.balanceState === "failed",
  );
  const allZero =
    allDone &&
    rows.every(
      (r) =>
        r.address === null ||
        (r.balanceState === "done" &&
          (r.positions ?? 0) === 0 &&
          (r.idle ?? 0) === 0),
    );

  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold">
          {t("tables.accounts.import.picker.heading")}
        </h3>
        <span className="text-xs text-muted-foreground">
          {t("tables.accounts.import.picker.subheading")}
        </span>
      </div>
      <ul className="divide-y divide-border rounded-md border" role="list">
        {rows.map((c) => (
          <li key={c.sigType}>
            <button
              type="button"
              onClick={() => {
                if (c.address === null) return;
                onPick({
                  sigType: c.sigType,
                  mode: c.mode,
                  address: c.address,
                  derived: c.derived,
                  ...(c.error ? { error: c.error } : {}),
                });
                setPicked(c);
              }}
              disabled={c.balanceState === "probing" || c.address === null}
              className="w-full text-left px-3 py-3 flex items-center gap-3
                         hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring
                         transition-colors min-h-[56px]
                         disabled:cursor-not-allowed disabled:opacity-60"
              aria-label={t("tables.accounts.import.picker.selectAria", {
                mode: t(modeLabelKey(c.mode)),
              })}
            >
              <span
                className={
                  hasBalance(c)
                    ? "h-1.5 w-1.5 rounded-full bg-gain shrink-0"
                    : "h-1.5 w-1.5 shrink-0"
                }
                aria-hidden
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span
                    className={`text-sm ${hasBalance(c) ? "font-semibold" : "font-medium"}`}
                  >
                    {t(modeLabelKey(c.mode))}
                  </span>
                  <span className="font-mono text-xs text-muted-foreground truncate">
                    {c.address
                      ? shortAddress(c.address)
                      : t("tables.accounts.import.picker.noAddress")}
                  </span>
                </div>
                {c.balanceState === "probing" && (
                  <Skeleton className="h-3.5 w-32 mt-1" />
                )}
                {c.balanceState === "done" && c.address !== null && (
                  <div
                    className={`text-xs tabular-nums ${
                      hasBalance(c) ? "text-gain" : "text-muted-foreground"
                    }`}
                  >
                    <div>
                      {t("tables.accounts.import.picker.positionsLabel")}{" "}
                      {formatUsd(c.positions)}
                    </div>
                    <div>
                      {t("tables.accounts.import.picker.idleLabel")}{" "}
                      {formatUsd(c.idle)}
                    </div>
                  </div>
                )}
                {c.balanceState === "failed" && (
                  <p className="text-xs text-muted-foreground mt-1">
                    {t("tables.accounts.import.picker.balanceFailed")}
                  </p>
                )}
                {c.error && c.address === null && (
                  <p className="text-xs text-muted-foreground mt-1">
                    {c.error}
                  </p>
                )}
              </div>
            </button>
          </li>
        ))}
      </ul>
      {allZero && (
        <p className="text-xs text-muted-foreground">
          {t("tables.accounts.import.picker.allZeroNote")}
        </p>
      )}
    </section>
  );
}
