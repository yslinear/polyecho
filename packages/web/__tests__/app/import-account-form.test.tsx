import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

/**
 * ImportAccountForm — the operator-authorised key-import island AFTER the
 * progressive-disclosure refactor (informed-choice flow).
 *
 * These tests prove:
 *  - field ORDER: 帳戶名稱 → EOA → Funder → 簽章類型 → 私鑰 → 加密密語
 *    → 危險操作密碼 (Funder moved BEFORE sigType so operator can see its
 *    balance BEFORE picking a signature type)
 *  - both EOA and Funder trigger a read-only probeTotalBalance once they
 *    are valid EVM addresses, regardless of sigType (lets operator probe
 *    funder balance pre-commit)
 *  - the secrets section (私鑰 / 加密密語 / 危險操作密碼) is locked
 *    (inputs disabled + opacity-50) until the identity section is valid:
 *    type 0 needs only EOA; type 1/2/3 needs EOA + funder
 *  - the submit button is disabled until all fields are valid (no more
 *    out-of-order toast.error surprises — the gate is visual + structural)
 *  - the honest CLI-scope hint is present
 *  - submit STILL calls importAccount with the SAME args (unchanged gate)
 *  - probe failure NEVER blocks submit
 *
 * The Server Actions (`./actions`, `./balance`), sonner, and the router are
 * mocked — no DB, no network.
 */

const importAccount = vi.fn();
const deriveMagicProxyForCheck = vi.fn();
vi.mock("@/app/accounts/actions", () => ({
  importAccount: (...a: unknown[]) => importAccount(...a),
  deriveMagicProxyForCheck: (...a: unknown[]) =>
    deriveMagicProxyForCheck(...a),
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
const L_BAL_POSITIONS = t("tables.accounts.import.balancePositionsLabel");
const L_BAL_IDLE = t("tables.accounts.import.balanceIdleLabel");
const L_BAL_TOTAL = t("tables.accounts.import.balanceTotalLabel");
const NA = t("tables.accounts.import.balanceNa");
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
    probeTotalBalance.mockReset().mockResolvedValue({
      ok: true,
      positionsUsd: 4321,
      idleUsdc: 100,
      totalUsd: 4421,
      message: "",
    });
    deriveMagicProxyForCheck.mockReset().mockResolvedValue({
      ok: true,
      derived: "0xfaffc82a2d2c9b2d8a52fd2b66585b58572ec427",
    });
    toastSuccess.mockReset();
    toastError.mockReset();
    refresh.mockReset();
  });

  it("renders fields with Funder BEFORE sigType (informed-choice order)", async () => {
    await openForm();

    // Query each accessible control, then compare DOM order. Funder is now
    // BEFORE sigType so the operator can read its balance BEFORE picking
    // a signature type (delivers on the sigTypeHint promise).
    const name = screen.getByLabelText(L_NAME);
    const eoa = screen.getByLabelText(L_EOA);
    const funder = screen.getByLabelText(L_FUNDER);
    const sig = screen.getByLabelText(L_SIG);
    const pk = screen.getByLabelText(L_PK);
    const enc = screen.getByLabelText(L_ENC);
    const danger = screen.getByLabelText(L_DANGER);

    const order = [name, eoa, funder, sig, pk, enc, danger];
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

  it("probes the EOA balance once it is a valid address and shows 部位/閒置/合計 three lines", async () => {
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);

    await waitFor(
      () => expect(probeTotalBalance).toHaveBeenCalledWith(EOA),
      { timeout: 2000 }
    );
    // All three labelled lines render.
    await waitFor(() =>
      expect(
        screen.getByText(new RegExp(L_BAL_POSITIONS))
      ).toBeInTheDocument()
    );
    expect(screen.getByText(new RegExp(L_BAL_IDLE))).toBeInTheDocument();
    expect(screen.getByText(new RegExp(L_BAL_TOTAL))).toBeInTheDocument();
    // And each renders the correct formatted USD value.
    expect(screen.getByText(/\$4,321\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\$100\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\$4,421\.00/)).toBeInTheDocument();
  });

  it("renders 部位 $50, 閒置 N/A, 合計 $50 on a partial idle failure", async () => {
    probeTotalBalance.mockResolvedValue({
      ok: true,
      positionsUsd: 50,
      idleUsdc: null,
      totalUsd: 50,
      message: t("tables.accounts.import.balancePartialFailed"),
    });
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);

    await waitFor(
      () => expect(probeTotalBalance).toHaveBeenCalledWith(EOA),
      { timeout: 2000 }
    );
    // Both 部位 and 合計 are $50 — there are exactly two matches.
    await waitFor(() => {
      const hits = screen.getAllByText(/\$50\.00/);
      expect(hits.length).toBe(2);
    });
    // Idle is the failed half — renders the i18n N/A token.
    expect(screen.getByText(new RegExp(NA))).toBeInTheDocument();
  });

  it("probes the funder balance even at signature_type 0 (informed-choice flow)", async () => {
    // The pre-progressive-disclosure form disabled the funder field at
    // type 0; the new flow keeps it editable so the operator can SEE the
    // funder's balance BEFORE committing to a sigType. type 0 still
    // ignores funder at submit time — the server-side normalisation is
    // untouched (covered by the actions test).
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);
    await waitFor(() => expect(probeTotalBalance).toHaveBeenCalledWith(EOA));

    // Funder input is enabled and probed once it parses as a valid EVM.
    expect(screen.getByLabelText(L_FUNDER)).not.toBeDisabled();
    await user.type(screen.getByLabelText(L_FUNDER), FUNDER);
    await waitFor(
      () => expect(probeTotalBalance).toHaveBeenCalledWith(FUNDER),
      { timeout: 2000 }
    );
  });

  it("a partial-failure probe still lets a valid form submit", async () => {
    probeTotalBalance.mockResolvedValue({
      ok: true,
      positionsUsd: 50,
      idleUsdc: null,
      totalUsd: 50,
      message: t("tables.accounts.import.balancePartialFailed"),
    });
    const user = await openForm();

    await user.type(screen.getByLabelText(L_EOA), EOA);
    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "enc-pass");
    await user.type(screen.getByLabelText(L_DANGER), "danger-pass");
    await user.click(screen.getByRole("button", { name: BTN_SUBMIT }));

    await waitFor(() => expect(importAccount).toHaveBeenCalledTimes(1));
    expect(toastSuccess).toHaveBeenCalled();
  });

  it("a probe failure does NOT block a valid submit (best-effort only)", async () => {
    probeTotalBalance.mockResolvedValue({
      ok: false,
      positionsUsd: null,
      idleUsdc: null,
      totalUsd: null,
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

  it("submit button is disabled until ALL fields are valid (no out-of-order toast surprises)", async () => {
    // Pre-progressive-disclosure the form let an invalid EOA reach the
    // onSubmit handler and surface as toast.error. The new flow blocks
    // submission STRUCTURALLY — button stays disabled, so the form
    // can't be sent before the fields make sense. importAccount is
    // never called and no toast fires either way.
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), "0xnothex");
    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "e");
    await user.type(screen.getByLabelText(L_DANGER), "d");

    expect(
      screen.getByRole("button", { name: BTN_SUBMIT })
    ).toBeDisabled();
    expect(importAccount).not.toHaveBeenCalled();
  });

  it("secrets section is locked (inputs disabled) until the EOA is valid", async () => {
    // Identity gate: with no EOA typed, the 私鑰 / 加密密語 / 危險操作密碼
    // inputs must be visible but inert (so the operator anticipates the
    // next steps without filling them in the wrong order).
    await openForm();

    expect(screen.getByLabelText(L_PK)).toBeDisabled();
    expect(screen.getByLabelText(L_ENC)).toBeDisabled();
    expect(screen.getByLabelText(L_DANGER)).toBeDisabled();
  });

  it("secrets unlock once the EOA is valid (type 0 path — funder optional)", async () => {
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);

    await waitFor(() => {
      expect(screen.getByLabelText(L_PK)).not.toBeDisabled();
    });
    expect(screen.getByLabelText(L_ENC)).not.toBeDisabled();
    expect(screen.getByLabelText(L_DANGER)).not.toBeDisabled();
  });

  /**
   * P2 — CREATE2 cross-check (sig-type 1 only). Warn-only; the form NEVER
   * blocks submit on the result. Server-side `deriveMagicProxyForCheck` is
   * mocked so the test asserts the FORM behaviour, not the keccak math
   * (that is covered by the magic-proxy ↔ Python parity vector).
   */
  it("test_create2_mismatch_warning_shown — sig-type 1 + non-matching funder shows yellow note", async () => {
    // Operator typed an explicit funder that does NOT match the CREATE2
    // derivation. Form must surface the mismatch heading + the derived
    // address (so the operator can compare on polymarket.com/settings).
    deriveMagicProxyForCheck.mockResolvedValue({
      ok: true,
      derived: "0x" + "11".repeat(20), // ≠ FUNDER
    });

    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);
    await user.type(screen.getByLabelText(L_FUNDER), FUNDER);
    // Pick sig-type 1 (POLY_PROXY) — the only type that triggers the check.
    await user.click(screen.getByLabelText(L_SIG));
    await user.click(
      screen.getByRole("option", {
        name: t("tables.accounts.import.sigType1"),
      })
    );

    await waitFor(() =>
      expect(deriveMagicProxyForCheck).toHaveBeenCalledWith(EOA)
    );
    await waitFor(() =>
      expect(
        screen.getByText(
          t("tables.accounts.import.create2MismatchHeading")
        )
      ).toBeInTheDocument()
    );
    // The derived value is shown so the operator can cross-check it.
    expect(
      screen.getByText(/0x1{40}/i)
    ).toBeInTheDocument();
    // Hint copy present.
    expect(
      screen.getByText(t("tables.accounts.import.create2MismatchHint"))
    ).toBeInTheDocument();
  });

  it("CREATE2 cross-check shows a match note when funder == derived", async () => {
    const derived = "0xfaffc82a2d2c9b2d8a52fd2b66585b58572ec427";
    deriveMagicProxyForCheck.mockResolvedValue({ ok: true, derived });

    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);
    await user.type(screen.getByLabelText(L_FUNDER), derived);
    await user.click(screen.getByLabelText(L_SIG));
    await user.click(
      screen.getByRole("option", {
        name: t("tables.accounts.import.sigType1"),
      })
    );

    await waitFor(() =>
      expect(
        screen.getByText(t("tables.accounts.import.create2Match"))
      ).toBeInTheDocument()
    );
    // Mismatch heading is NOT shown when the values match.
    expect(
      screen.queryByText(
        t("tables.accounts.import.create2MismatchHeading")
      )
    ).not.toBeInTheDocument();
  });

  it("CREATE2 cross-check is NOT triggered for sig-type 0 (no warning, no derive call)", async () => {
    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);
    await user.type(screen.getByLabelText(L_FUNDER), FUNDER);
    // sig-type stays 0 (default) — the check must be inert.

    // Wait long enough for any erroneous trigger; assert no call.
    await new Promise((r) => setTimeout(r, 50));
    expect(deriveMagicProxyForCheck).not.toHaveBeenCalled();
    expect(
      screen.queryByText(
        t("tables.accounts.import.create2MismatchHeading")
      )
    ).not.toBeInTheDocument();
  });

  it("CREATE2 mismatch does NOT block submit (warn-only)", async () => {
    deriveMagicProxyForCheck.mockResolvedValue({
      ok: true,
      derived: "0x" + "11".repeat(20),
    });

    const user = await openForm();
    await user.type(screen.getByLabelText(L_EOA), EOA);
    await user.type(screen.getByLabelText(L_FUNDER), FUNDER);
    await user.click(screen.getByLabelText(L_SIG));
    await user.click(
      screen.getByRole("option", {
        name: t("tables.accounts.import.sigType1"),
      })
    );
    await waitFor(() =>
      expect(
        screen.getByText(
          t("tables.accounts.import.create2MismatchHeading")
        )
      ).toBeInTheDocument()
    );

    // Fill secrets and submit — mismatch must NOT prevent the call.
    await user.type(screen.getByLabelText(L_PK), PK);
    await user.type(screen.getByLabelText(L_ENC), "enc-pass");
    await user.type(screen.getByLabelText(L_DANGER), "danger-pass");
    await user.click(screen.getByRole("button", { name: BTN_SUBMIT }));

    await waitFor(() => expect(importAccount).toHaveBeenCalledTimes(1));
    expect(importAccount).toHaveBeenCalledWith(
      expect.objectContaining({
        eoaAddress: EOA,
        funder: FUNDER,
        signatureType: 1,
      })
    );
  });
});
