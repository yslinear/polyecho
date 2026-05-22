import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

/**
 * ImportAccountForm — INTEGRATION test: form ↔ FunderPicker ↔ submit.
 *
 * The per-layer unit tests each mock the layer below them — in
 * `import-account-form.test.tsx` the FunderPicker is rendered but
 * `deriveAllFunders` is stubbed to {ok:false}, so the picker is inert and
 * the picker→form wiring is never exercised. This test fills that gap: it
 * renders the REAL form with the REAL FunderPicker and only mocks the leaf
 * Server Actions (`deriveAllFunders`, `probeTotalBalance`, `importAccount`,
 * `deriveMagicProxyForCheck`). It then drives the whole path:
 *
 *   open form → type EOA → click 查詢錢包餘額 → click a picker row →
 *   the picked sigType + funder land in the form fields → fill secrets →
 *   submit → importAccount receives the picked values.
 *
 * What this proves that the unit tests cannot: the picker's `onPick`
 * actually flows through the form's `handlePick` into the sigType/funder
 * state and into the final `importAccount` call.
 */

const importAccount = vi.fn();
const deriveMagicProxyForCheck = vi.fn();
const deriveAllFunders = vi.fn();
vi.mock("@/app/accounts/actions", () => ({
  importAccount: (...a: unknown[]) => importAccount(...a),
  deriveMagicProxyForCheck: (...a: unknown[]) => deriveMagicProxyForCheck(...a),
  deriveAllFunders: (...a: unknown[]) => deriveAllFunders(...a),
}));

const probeTotalBalance = vi.fn();
vi.mock("@/app/accounts/balance", () => ({
  probeTotalBalance: (...a: unknown[]) => probeTotalBalance(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

import { ImportAccountForm } from "@/app/accounts/import-account-form";

// i18n-derived locators.
const OPEN = t("tables.accounts.import.openButton");
const L_EOA = t("tables.accounts.import.eoaLabel");
const L_FUNDER = t("tables.accounts.import.funderLabel");
const L_PK = t("tables.accounts.import.privateKeyLabel");
const L_ENC = t("tables.accounts.import.encPassLabel");
const L_DANGER = t("tables.accounts.import.dangerPassLabel");
const BTN_SUBMIT = t("tables.accounts.import.submit");
const DETECT_BTN = t("tables.accounts.import.picker.detectButton");
const MODE_EOA = t("tables.accounts.import.picker.modeEoa");
const MODE_MAGIC = t("tables.accounts.import.picker.modeMagic");

const EOA = "0x" + "ab".repeat(20);
const MAGIC = "0x" + "cd".repeat(20);
const SAFE = "0x" + "12".repeat(20);
const DEPOSIT = "0x" + "34".repeat(20);
const PK = "0x" + "ef".repeat(32);

/** The 4 candidates `deriveAllFunders` resolves to in this test. */
function mkCandidates() {
  return {
    ok: true as const,
    candidates: [
      { sigType: 0, mode: "eoa", address: EOA, derived: false },
      { sigType: 1, mode: "magic", address: MAGIC, derived: true },
      { sigType: 2, mode: "safe", address: SAFE, derived: true },
      { sigType: 3, mode: "poly_1271", address: DEPOSIT, derived: true },
    ],
  };
}

function mkBalance(positions: number, idle: number) {
  return {
    ok: true,
    positionsUsd: positions,
    idleUsdc: idle,
    totalUsd: positions + idle,
    message: "",
  };
}

/** Open the form, type the EOA, click 查詢錢包餘額, wait for the 4 picker
 *  rows to finish probing (enabled). Returns the userEvent instance. */
async function openAndDetect() {
  const user = userEvent.setup();
  render(<ImportAccountForm />);
  await user.click(screen.getByRole("button", { name: OPEN }));
  await user.type(screen.getByLabelText(L_EOA), EOA);

  // The picker appears once the EOA is valid; click its lookup button.
  await user.click(await screen.findByRole("button", { name: DETECT_BTN }));

  // Wait until the Magic Proxy picker row is present AND enabled (probe done).
  await waitFor(
    () => {
      const row = screen.getByRole("button", {
        name: new RegExp(MODE_MAGIC),
      });
      expect(row).not.toBeDisabled();
    },
    { timeout: 3000 },
  );
  return user;
}

beforeEach(() => {
  importAccount.mockReset().mockResolvedValue({
    ok: true,
    message: "已建立帳戶 acct（狀態 → ready）。",
    action: "created",
  });
  deriveAllFunders.mockReset().mockResolvedValue(mkCandidates());
  probeTotalBalance.mockReset().mockResolvedValue(mkBalance(50, 10));
  deriveMagicProxyForCheck.mockReset().mockResolvedValue({
    ok: true,
    derived: MAGIC,
  });
  toastSuccess.mockReset();
  toastError.mockReset();
  refresh.mockReset();
});

describe("ImportAccountForm — picker → form → submit integration", () => {
  it("picking the Magic Proxy row fills sigType + funder, and submit sends them to importAccount", async () => {
    const user = await openAndDetect();

    // Click the Magic Proxy (sig-1) picker row.
    await user.click(
      screen.getByRole("button", { name: new RegExp(MODE_MAGIC) }),
    );

    // The pick flowed into the form's Funder field.
    await waitFor(() => {
      expect(screen.getByLabelText(L_FUNDER)).toHaveValue(MAGIC);
    });

    // Fill the secrets section + submit.
    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "enc-pass");
    await user.type(screen.getByLabelText(L_DANGER), "danger-pass");
    await user.click(screen.getByRole("button", { name: BTN_SUBMIT }));

    // importAccount received the PICKED sigType (1) + funder (MAGIC).
    await waitFor(() => expect(importAccount).toHaveBeenCalledTimes(1));
    expect(importAccount).toHaveBeenCalledWith(
      expect.objectContaining({
        eoaAddress: EOA,
        privateKey: PK,
        signatureType: 1,
        funder: MAGIC,
        encryptionPassphrase: "enc-pass",
        dangerPassphrase: "danger-pass",
      }),
    );
    expect(toastSuccess).toHaveBeenCalled();
  });

  it("picking the EOA-self row fills sigType 0 with the EOA as funder", async () => {
    const user = await openAndDetect();

    await user.click(
      screen.getByRole("button", { name: new RegExp(MODE_EOA) }),
    );

    await waitFor(() => {
      expect(screen.getByLabelText(L_FUNDER)).toHaveValue(EOA);
    });

    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "enc-pass");
    await user.type(screen.getByLabelText(L_DANGER), "danger-pass");
    await user.click(screen.getByRole("button", { name: BTN_SUBMIT }));

    await waitFor(() => expect(importAccount).toHaveBeenCalledTimes(1));
    expect(importAccount).toHaveBeenCalledWith(
      expect.objectContaining({
        eoaAddress: EOA,
        signatureType: 0,
        funder: EOA,
      }),
    );
  });

  it("reselecting a different row overrides the first pick before submit", async () => {
    const user = await openAndDetect();

    // First pick: Magic Proxy.
    await user.click(
      screen.getByRole("button", { name: new RegExp(MODE_MAGIC) }),
    );
    await waitFor(() => {
      expect(screen.getByLabelText(L_FUNDER)).toHaveValue(MAGIC);
    });

    // Reselect → the picker re-opens.
    await user.click(screen.getByRole("button", { name: /重選/ }));
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: new RegExp(MODE_EOA) }),
      ).not.toBeDisabled();
    });

    // Second pick: EOA self — this must win.
    await user.click(
      screen.getByRole("button", { name: new RegExp(MODE_EOA) }),
    );
    await waitFor(() => {
      expect(screen.getByLabelText(L_FUNDER)).toHaveValue(EOA);
    });

    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "enc-pass");
    await user.type(screen.getByLabelText(L_DANGER), "danger-pass");
    await user.click(screen.getByRole("button", { name: BTN_SUBMIT }));

    await waitFor(() => expect(importAccount).toHaveBeenCalledTimes(1));
    // The LAST pick (sig 0 / EOA) is what submits, not the first.
    expect(importAccount).toHaveBeenCalledWith(
      expect.objectContaining({ signatureType: 0, funder: EOA }),
    );
  });

  it("does not call deriveAllFunders until 查詢錢包餘額 is clicked", async () => {
    const user = userEvent.setup();
    render(<ImportAccountForm />);
    await user.click(screen.getByRole("button", { name: OPEN }));
    await user.type(screen.getByLabelText(L_EOA), EOA);

    // Picker is visible (EOA valid) but detection has not fired.
    const detectBtn = await screen.findByRole("button", { name: DETECT_BTN });
    expect(deriveAllFunders).not.toHaveBeenCalled();

    await user.click(detectBtn);
    await waitFor(() => {
      expect(deriveAllFunders).toHaveBeenCalledWith(EOA);
    });
  });
});
