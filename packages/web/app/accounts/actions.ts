"use server";

/**
 * Account private-key import — Server Action (PRD §5.1).
 *
 * *** SECURITY-MODEL OVERRIDE (recorded) ***
 * PolyEcho's default model is CLI-ONLY private-key import (the web tier is
 * read-only; `packages/web/app/accounts/page.tsx` still states this). The
 * operator has EXPLICITLY + knowingly authorised overriding that isolation
 * so a key can be imported from the web `/accounts` page. This action is
 * that override. It is gated by the SAME operator danger-passphrase proof
 * the §5.7 危险-tier flow uses (alerts `dangerGate`), and writes the
 * private key ONLY as the FROZEN AES-256-GCM v1 blob trader-core's frozen
 * Python `decrypt_private_key` can read (see `lib/key-crypto.ts`).
 *
 * Flow (all gates FAIL CLOSED; key material NEVER echoed/logged):
 *   1. Validate inputs (EOA address shape, private-key hex length,
 *      signature_type ∈ 0..3, funder shape) — cheap rejects BEFORE any
 *      crypto / DB work.
 *   2. Resolve funder per the CLI `polyecho account import` rules
 *      (account.py): type 0 → funder = the EOA itself (always exact);
 *      types 1/2/3 → REQUIRE an explicit, on-chain-verified `funder`
 *      (see the SIGNATURE-TYPE SCOPE note below).
 *   3. Danger gate: `settings.danger_passphrase_hash` must be provisioned
 *      (CLI `polyecho account set-danger-passphrase`); verify the submitted
 *      passphrase against that FROZEN canonical `pbkdf2$…` string via the
 *      EXISTING `lib/passphrase.ts` `verifyPassphrase` (constant-time;
 *      rejects sub-100000-iter / tampered strings). NEVER provisions it.
 *   4. Encrypt the key with `encryptPrivateKey` (byte-identical to Python
 *      `encrypt_private_key`, blob v1).
 *   5. Drizzle UPSERT the `accounts` row by the UNIQUE `eoa_address`
 *      (mirrors the CLI's session upsert): set `encrypted_private_key`
 *      (bytea), `signature_type`, `funder_address`, and transition
 *      `status` `pending_key_import` → `ready` (CLI status semantics).
 *   6. Return a SAFE result — never the key, passphrase, or blob.
 *
 * SIGNATURE-TYPE SCOPE (honest — matches the CLI's safety stance,
 * account.py / accounts.py P0-5):
 *   - type 0 (EOA): fully supported. funder == signer (exact, no
 *     derivation). This is the primary, fully-verified path.
 *   - type 1 (POLY_PROXY): the CLI auto-derives the Magic proxy via the
 *     FROZEN Python `magic_proxy_address` (keccak/CREATE2 over brief §9
 *     constants). Re-implementing that CREATE2 in TS WITHOUT those frozen
 *     constants would risk a divergent (wrong) funder — worse than
 *     refusing. So type 1 here REQUIRES an explicit operator-supplied
 *     `funder` (on-chain-verified) rather than a fabricated derivation.
 *   - type 2 (POLY_GNOSIS_SAFE) / 3 (POLY_1271): the CLI itself has NO
 *     trustworthy offline derivation and MANDATES an explicit
 *     on-chain-verified `--funder` (account.py refuses otherwise). Same
 *     rule enforced here.
 *   This deliberately refuses to persist a guessed funder (writing a wrong
 *   funder is more dangerous than refusing — runbook §2.3).
 */

import { sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { accounts } from "@/lib/db/schema";
import { loadDangerPassphraseHash, verifyPassphrase } from "@/lib/passphrase";
import { encryptPrivateKey } from "@/lib/key-crypto";
import { EVM_ADDRESS_RE, PRIVATE_KEY_RE } from "@/lib/format";
import { magicProxyAddress } from "@/lib/magic-proxy";
import { t, type I18nKey } from "@/lib/i18n";

/** Mirrors `polyecho.shared.accounts.SIGNATURE_TYPE_NAMES` (0..3). */
const SIGNATURE_TYPE_NAMES: Record<number, string> = {
  0: "EOA",
  1: "POLY_PROXY",
  2: "POLY_GNOSIS_SAFE",
  3: "POLY_1271",
};

export interface ImportAccountInput {
  /** EOA / signer address (0x + 40 hex). Stored lowercased (CLI parity). */
  eoaAddress: string;
  /** secp256k1 private key (optional 0x + 64 hex). NEVER logged/echoed. */
  privateKey: string;
  /** Polymarket signature type: 0 EOA | 1 POLY_PROXY | 2 SAFE | 3 1271. */
  signatureType: number;
  /** Explicit on-chain-verified funder (0x+40hex). REQUIRED for type 1/2/3. */
  funder?: string;
  /** Friendly account name; defaults to the lowercased EOA (CLI parity). */
  name?: string;
  /** Operator danger passphrase — gate (NOT the encryption passphrase). */
  dangerPassphrase: string;
  /** Encryption passphrase for the key blob (the one trader-core will use). */
  encryptionPassphrase: string;
}

export interface ImportAccountResult {
  ok: boolean;
  /** zh-TW operator message. NEVER contains key/passphrase/blob material. */
  message: string;
  /** "created" | "updated" on success (mirrors the CLI's action word). */
  action?: "created" | "updated";
}

/**
 * importAccount — the recorded CLI-isolation override. Returns a SAFE
 * result object; throws nothing that could carry secret material.
 */
export async function importAccount(
  input: ImportAccountInput
): Promise<ImportAccountResult> {
  // ---- 1. Cheap input validation (BEFORE crypto / DB) -------------------
  const eoaRaw = (input?.eoaAddress ?? "").trim();
  const pkRaw = (input?.privateKey ?? "").trim();
  const sigType = Number(input?.signatureType);
  const funderRaw = (input?.funder ?? "").trim();
  const nameRaw = (input?.name ?? "").trim();
  const dangerPass = input?.dangerPassphrase ?? "";
  const encPass = input?.encryptionPassphrase ?? "";

  if (!EVM_ADDRESS_RE.test(eoaRaw)) {
    return { ok: false, message: "EOA 位址格式無效（需為 0x 加 40 個十六進位字元）。" };
  }
  if (!PRIVATE_KEY_RE.test(pkRaw)) {
    // NEVER echo the key — only describe the expected shape.
    return {
      ok: false,
      message: "私鑰格式無效（需為 64 個十六進位字元，可選 0x 前綴）。",
    };
  }
  if (!Number.isInteger(sigType) || sigType < 0 || sigType > 3) {
    return { ok: false, message: "簽章類型無效（須為 0、1、2 或 3）。" };
  }
  if (dangerPass.length < 1) {
    return { ok: false, message: "請輸入危險操作確認密碼。" };
  }
  if (encPass.length < 1) {
    return { ok: false, message: "請輸入私鑰加密密語。" };
  }

  const eoaLower = eoaRaw.toLowerCase();

  // ---- 2. Funder resolution (CLI account.py parity) --------------------
  let funderAddr: string;
  if (sigType === 0) {
    // Type 0 EOA: funder == signer. Always exact (no derivation).
    funderAddr = eoaLower;
  } else {
    // Types 1/2/3: the web refuses to fabricate a funder (see header
    // SIGNATURE-TYPE SCOPE). An explicit, on-chain-verified address is
    // mandatory — exactly the CLI's stance for 2/3, and the safe stance
    // for 1 (the frozen CREATE2 constants are not re-implemented here).
    if (!funderRaw) {
      return {
        ok: false,
        message:
          `簽章類型 ${sigType} (${SIGNATURE_TYPE_NAMES[sigType] ?? "?"}) ` +
          "需由操作員提供經鏈上驗證的 funder 位址。" +
          "（type 1 的 CREATE2 推導僅在 CLI 端有可信實作；type 2/3 無離線推導。" +
          "請改用 CLI `polyecho account import`，或在此顯式填入已驗證的 funder。）",
      };
    }
    if (!EVM_ADDRESS_RE.test(funderRaw)) {
      return { ok: false, message: "funder 位址格式無效（需為 0x 加 40 個十六進位字元）。" };
    }
    funderAddr = funderRaw.toLowerCase();
  }

  const accountName = nameRaw || eoaLower;

  // ---- 3. Danger gate — operator passphrase proof (FAIL CLOSED) --------
  // Same gate as the §5.7 危险-tier flow: read the operator-provisioned
  // `settings.danger_passphrase_hash`; absent => refuse (NEVER provision);
  // constant-time verify via the existing lib/passphrase verifier.
  const storedHash = await loadDangerPassphraseHash();
  if (!storedHash) {
    return {
      ok: false,
      message:
        "危險操作密碼尚未設定，請先用 CLI `polyecho account set-danger-passphrase` 佈建後再試。",
    };
  }
  const passOk = await verifyPassphrase(dangerPass, storedHash);
  if (!passOk) {
    return { ok: false, message: "危險操作確認密碼錯誤，已中止匯入。" };
  }

  // ---- 4. Encrypt (byte-identical to Python encrypt_private_key v1) ----
  let encrypted: Buffer;
  try {
    encrypted = await encryptPrivateKey(pkRaw, encPass);
  } catch {
    // encryptPrivateKey only throws generic empty/oversize-config errors
    // (no secret material), but swallow defensively so nothing leaks.
    return { ok: false, message: "私鑰加密失敗，已中止匯入。" };
  }

  // ---- 5. Drizzle UPSERT accounts (CLI session-upsert parity) ----------
  // ONE atomic statement keyed by the UNIQUE eoa_address — no TOCTOU
  // SELECT-then-(insert|update) race. Mirrors account.py: insert a new row
  // OR update the existing one (key/sig/funder), transitioning status
  // pending_key_import -> ready. The Postgres `xmax = 0` RETURNING idiom
  // reports which branch ran so the operator message still distinguishes
  // 建立 vs 更新 (the CLI's action word); a mislabel is at worst cosmetic.
  try {
    const [row] = await db
      .insert(accounts)
      .values({
        name: accountName,
        eoa_address: eoaLower,
        funder_address: funderAddr,
        signature_type: sigType,
        encrypted_private_key: encrypted,
        status: "ready",
      })
      .onConflictDoUpdate({
        target: accounts.eoa_address,
        set: {
          name: accountName,
          funder_address: funderAddr,
          signature_type: sigType,
          encrypted_private_key: encrypted,
          status: "ready",
          updated_at: new Date(),
        },
      })
      .returning({ inserted: sql<boolean>`(xmax = 0)` });

    return (row?.inserted ?? true)
      ? {
          ok: true,
          action: "created",
          message: `已建立帳戶 ${accountName}（狀態 pending_key_import → ready）。私鑰已以 AES-256-GCM 加密儲存，永不顯示。`,
        }
      : {
          ok: true,
          action: "updated",
          message: `已更新帳戶 ${accountName}（狀態 → ready）。私鑰已以 AES-256-GCM 加密儲存，永不顯示。`,
        };
  } catch {
    // DB errors must not leak the (already-encrypted) blob or inputs.
    return { ok: false, message: "寫入帳戶失敗，請稍後重試。" };
  }
}

// ---------------------------------------------------------------------------
// P2: CREATE2 cross-check helper (warn-only; never blocks).
// ---------------------------------------------------------------------------
export interface MagicProxyCheck {
  /** True when the EOA was a valid 0x+40-hex shape (else `derived` is null). */
  ok: boolean;
  /** Lowercased CREATE2 Magic-proxy derivation, or null on invalid input. */
  derived: string | null;
}

/**
 * deriveMagicProxyForCheck — pure, side-effect-free server-side derivation
 * used by the import form's sig-type-1 CREATE2 cross-check.
 *
 * This is intentionally a Server Action (rather than a client-side helper)
 * because the web bundle has no keccak256 / viem / ethers dependency; the
 * derivation lives in `lib/magic-proxy.ts` (a TS byte-mirror of the FROZEN
 * Python `magic_proxy_address`). The result is RENDERED ONLY — never
 * persisted, never used to override the operator-typed funder. A mismatch
 * is a WARNING in the form, not a block.
 */
export async function deriveMagicProxyForCheck(
  eoa: string
): Promise<MagicProxyCheck> {
  const trimmed = (eoa ?? "").trim();
  if (!EVM_ADDRESS_RE.test(trimmed)) {
    return { ok: false, derived: null };
  }
  try {
    return { ok: true, derived: magicProxyAddress(trimmed.toLowerCase()) };
  } catch {
    return { ok: false, derived: null };
  }
}

// ===========================================================================
// deriveAllFunders — Server Action proxy to the internal `detect` container.
// ===========================================================================
//
// The `detect` service (separate docker container, internal docker network
// only, no public port) imports the FROZEN Python `polyecho.shared.polymarket.wallet`
// derivations and returns the 4 candidate funder addresses for an EOA. This
// Server Action is the web's entry point; the UI (`funder-picker.tsx`) calls
// it and then runs `probeTotalBalance` on each address.
//
// SECURITY MODEL: The web tier still does NOT itself derive the type-1/2/3
// CREATE2 / RPC addresses. The derivations live in the trusted Python source
// of truth; this action only PROXIES. The returned addresses are RENDERED
// ONLY and never persisted (the picker click pre-fills the form, but the
// operator still confirms via submit, which goes through the existing
// `importAccount` gates).
//
// FAIL-SOFT: timeout (3s) / network / parse / sig2 RPC failure all resolve
// to {ok:false, reason:<zh-TW>}. The picker then shows a fallback message and
// the user can still manually proceed. No throws.
//
// i18n NOTE: the `tables.accounts.import.picker.*` keys below are added by
// Agent C (parallel). To keep this typecheck-clean NOW, the literals are
// cast through `I18nKey`; `t()` returns the key itself when missing (see
// lib/i18n.ts — "Missing key → returns the key itself"), so the action stays
// safe even before Agent C lands the strings.

export type FunderCandidate = {
  sigType: 0 | 1 | 2 | 3;
  mode: "eoa" | "magic" | "safe" | "poly_1271";
  address: string | null;
  derived: boolean;
  error?: string;
};

export type DeriveAllFundersResult =
  | { ok: true; candidates: FunderCandidate[] }
  | { ok: false; reason: string };

const DETECT_URL = process.env.POLYECHO_DETECT_URL ?? "http://detect:8001";
const DETECT_TIMEOUT_MS = 3000;

export async function deriveAllFunders(
  eoa: string,
): Promise<DeriveAllFundersResult> {
  const trimmed = (eoa ?? "").trim();
  if (!EVM_ADDRESS_RE.test(trimmed)) {
    return {
      ok: false,
      reason: t("tables.accounts.import.picker.errInvalidEoa" as I18nKey),
    };
  }
  const url = `${DETECT_URL}/detect?eoa=${encodeURIComponent(trimmed.toLowerCase())}`;
  try {
    const res = await fetch(url, {
      signal: AbortSignal.timeout(DETECT_TIMEOUT_MS),
      cache: "no-store",
    });
    if (!res.ok) {
      return {
        ok: false,
        reason: t("tables.accounts.import.picker.errServiceDown" as I18nKey),
      };
    }
    const body = (await res.json()) as unknown;
    if (body == null || typeof body !== "object") {
      return {
        ok: false,
        reason: t("tables.accounts.import.picker.errServiceDown" as I18nKey),
      };
    }
    const b = body as Record<string, unknown>;
    // Map detect response → ordered candidates list (sig0,1,2,3). Validate
    // each field defensively; a malformed sub-object yields a candidate
    // with address:null + derived:false (the picker handles that fallback).
    const candidates: FunderCandidate[] = [
      mapCandidate(0, b.sig0),
      mapCandidate(1, b.sig1),
      mapCandidate(2, b.sig2),
      mapCandidate(3, b.sig3),
    ];
    return { ok: true, candidates };
  } catch {
    // Timeout / network / JSON-parse — all collapse to a single fail-soft
    // result. DO NOT log the EOA (operator privacy + security model).
    return {
      ok: false,
      reason: t("tables.accounts.import.picker.errServiceDown" as I18nKey),
    };
  }
}

function mapCandidate(
  sigType: 0 | 1 | 2 | 3,
  raw: unknown,
): FunderCandidate {
  const r =
    raw && typeof raw === "object"
      ? (raw as {
          addr?: string | null;
          mode?: string;
          derived?: boolean;
          error?: string;
        })
      : null;
  const mode = (r?.mode ?? sigTypeMode(sigType)) as FunderCandidate["mode"];
  const address =
    typeof r?.addr === "string" && r.addr.length > 0 ? r.addr : null;
  return {
    sigType,
    mode,
    address,
    derived: Boolean(r?.derived),
    ...(typeof r?.error === "string" && r.error.length > 0
      ? { error: r.error }
      : {}),
  };
}

function sigTypeMode(sigType: 0 | 1 | 2 | 3): FunderCandidate["mode"] {
  return (["eoa", "magic", "safe", "poly_1271"] as const)[sigType];
}
