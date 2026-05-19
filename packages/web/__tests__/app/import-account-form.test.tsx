import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

/**
 * ImportAccountForm — the operator-authorised key-import island, AFTER the
 * UX reorder + the additive read-only balance probe.
 *
 * These tests prove:
 *  - field ORDER: 帳戶名稱 → EOA → 簽章類型 → Funder → 私鑰 → 加密密語
 *    → 危險操作密碼 (signature type is now near the END, not upfront)
 *  - a valid EOA triggers the read-only probeBalance and renders the
 *    balance line (with the "this IS the type-0 account value" note)
 *  - the funder is probed only when signature_type ≠ 0
 *  - the honest CLI-scope hint is present
 *  - submit STILL calls importAccount with the SAME args (unchanged gate)
 *  - probe failure NEVER blocks submit
 *
 * The Server Actions (`./actions`, `./balance`), sonner, and the router are
 * mocked — no DB, no network.
 */

const importAccount = vi.fn();
vi.mock("@/app/accounts/actions", () => ({
  importAccount: (...a: unknown[]) => importAccount(...a),
}));

const probeBalance = vi.fn();
vi.mock("@/app/accounts/balance", () => ({
  probeBalance: (...a: unknown[]) => probeBalance(...a),
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

// i18n-derived locators (assert structure, survive copy edits).
const OPEN = t("tables.accounts.import.openButton");
const L_NAME = t("tables.accounts.import.nameLabel");
const L_EOA = t("tables.accounts.import.eoaLabel");
const L_SIG = t("tables.accounts.import.sigTypeLabel");
const L_FUNDER = t("tables.accounts.import.funderLabel");
const L_PK = t("tables.accounts.import.privateKeyLabel");
const L_ENC = t("tables.accounts.import.encPassLabel");
const L_DANGER = t("tables.accounts.import.dangerPassLabel");
const BTN_SUBMIT = t("tables.accounts.import.submit");
const BALANCE_LABEL = t("tables.accounts.import.balanceLabel");
const CLI_HINT = t("tables.accounts.import.balanceCliHint");

const EOA = "0x" + "ab".repeat(20);
const FUNDER = "0x" + "cd".repeat(20);
const PK = "0x" + "ef".repeat(32);

async function openForm() {
  const user = userEvent.setup();
  render(<ImportAccountForm />);
  await user.click(screen.getByRole("button", { name: OPEN }));
  return user;
}

describe("ImportAccountForm (reordered + read-only balance probe)", () => {
  beforeEach(() => {
    importAccount.mockReset().mockResolvedValue({
      ok: true,
      message: "已建立帳戶 acct（狀態 → ready）。",
      action: "created",
    });
    probeBalance
      .mockReset()
      .mockResolvedValue({ ok: true, valueUsd: 4321, message: "" });
    toastSuccess.mockReset();
    toastError.mockReset();
    refresh.mockReset();
  });

  it("renders fields with signature type AFTER the EOA (informed order)", async () => {
    await openForm();

    // Query each accessible control, then compare DOM order.
    const name = screen.getByLabelText(L_NAME);
    const eoa = screen.getByLabelText(L_EOA);
    const sig = screen.getByLabelText(L_SIG);
    const funder = screen.getByLabelText(L_FUNDER);
    const pk = screen.getByLabelText(L_PK);
    const enc = screen.getByLabelText(L_ENC);
    const danger = screen.getByLabelText(L_DANGER);

    const order = [name, eoa, sig, funder, pk, enc, danger];
    for (let i = 1; i < order.length; i++) {
      // Node.DOCUMENT_POSITION_FOLLOWING (4) === order[i] comes AFTER order[i-1]
      expect(
        order[i - 1].compareDocumentPosition(order[i]) &
          Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy();
    }
    // Honest scope hint present.
    expect(screen.getByText(CLI_HINT)).toBeInTheDocument();
  });

  it("probes the EOA balance once it is a valid address and shows it", async () => {
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);

    await waitFor(
      () => expect(probeBalance).toHaveBeenCalledWith(EOA),
      { timeout: 2000 }
    );
    // The read-only balance line renders the formatted value + label.
    await waitFor(() =>
      expect(
        screen.getByText(new RegExp(BALANCE_LABEL))
      ).toBeInTheDocument()
    );
    expect(screen.getByText(/\$4,321\.00/)).toBeInTheDocument();
  });

  it("does NOT probe the funder for signature_type 0 (EOA == funder)", async () => {
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);
    await waitFor(() => expect(probeBalance).toHaveBeenCalledWith(EOA));

    // type 0: funder input is disabled and never probed.
    expect(screen.getByLabelText(L_FUNDER)).toBeDisabled();
    expect(probeBalance).not.toHaveBeenCalledWith(FUNDER);
  });

  it("a probe failure does NOT block a valid submit (best-effort only)", async () => {
    probeBalance.mockResolvedValue({
      ok: false,
      valueUsd: null,
      message: t("tables.accounts.import.balanceFailed"),
    });
    const user = await openForm();

    await user.type(screen.getByLabelText(L_EOA), EOA);
    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "enc-pass");
    await user.type(screen.getByLabelText(L_DANGER), "danger-pass");
    await user.click(screen.getByRole("button", { name: BTN_SUBMIT }));

    await waitFor(() => expect(importAccount).toHaveBeenCalledTimes(1));
    expect(importAccount).toHaveBeenCalledWith({
      eoaAddress: EOA,
      privateKey: PK,
      signatureType: 0,
      funder: "",
      name: "",
      dangerPassphrase: "danger-pass",
      encryptionPassphrase: "enc-pass",
    });
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalled();
  });

  it("client-side EOA validation still gates submit (unchanged)", async () => {
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), "0xnothex");
    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "e");
    await user.type(screen.getByLabelText(L_DANGER), "d");
    await user.click(screen.getByRole("button", { name: BTN_SUBMIT }));

    expect(importAccount).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalledWith(
      t("tables.accounts.import.errEoa")
    );
  });
});
