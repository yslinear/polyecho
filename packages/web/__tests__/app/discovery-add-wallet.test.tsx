import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * AddWalletDialog island — operator-facing manual-add UX.
 *
 * The four contracts:
 *   1. Click the header button opens the Dialog.
 *   2. An invalid address keeps Submit disabled (the action is NOT called)
 *      and renders the inline error after the field is touched.
 *   3. A successful add closes the Dialog and fires the success toast.
 *   4. An `already_tracked` response keeps the Dialog open and renders the
 *      "已在追蹤(目前狀態:<state>)" hint with the existing lifecycle_state.
 *
 * The Server Action and `sonner` are mocked; @/lib/db is mocked because the
 * action module imports the Drizzle client at the top level.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

type ActionResult =
  | { ok: true }
  | { ok: false; reason: "invalid_address" }
  | { ok: false; reason: "already_tracked"; lifecycle_state: string }
  | { ok: false; reason: "error" };

let nextResult: ActionResult = { ok: true };
const addCandidateLeader = vi.fn(async (): Promise<ActionResult> => nextResult);
vi.mock("@/app/discovery/actions", () => ({
  addCandidateLeader: (...a: unknown[]) => addCandidateLeader(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { AddWalletDialog } from "@/app/discovery/add-wallet-dialog";
import { t } from "@/lib/i18n";

const BUTTON_LABEL = t("discovery.list.addWallet.button");
const DIALOG_TITLE = t("discovery.list.addWallet.title");
const ADDR_LABEL = t("discovery.list.addWallet.addressLabel");
const NOTE_LABEL = t("discovery.list.addWallet.noteLabel");
const SUBMIT = t("discovery.list.addWallet.submit");
const CANCEL = t("discovery.list.addWallet.cancel");
const ADDR_ERROR = t("discovery.list.addWallet.addressError");
const SUCCESS = t("discovery.list.addWallet.successToast");
const DUP_PAPER = t("discovery.list.addWallet.duplicate", { state: "paper" });

const VALID_ADDR = "0xabcdef0123456789abcdef0123456789abcdef01";

beforeEach(() => {
  refresh.mockClear();
  addCandidateLeader.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
  nextResult = { ok: true };
});

describe("AddWalletDialog — open + invalid-input", () => {
  it("opens the Dialog when the header button is clicked", async () => {
    render(<AddWalletDialog />);
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: BUTTON_LABEL }));
    expect(
      await screen.findByRole("heading", { name: DIALOG_TITLE }),
    ).toBeInTheDocument();
    // The two fields are labelled and reachable by accessible name.
    expect(screen.getByLabelText(ADDR_LABEL)).toBeInTheDocument();
    expect(screen.getByLabelText(NOTE_LABEL)).toBeInTheDocument();
  });

  it("keeps Submit disabled for an invalid address and shows the inline error after blur", async () => {
    render(<AddWalletDialog />);
    await userEvent.click(screen.getByRole("button", { name: BUTTON_LABEL }));
    const addr = screen.getByLabelText(ADDR_LABEL);
    await userEvent.type(addr, "not-an-address");
    // Submit is disabled, so clicking does nothing — and the action is NEVER called.
    const submit = screen.getByRole("button", { name: SUBMIT });
    expect(submit).toBeDisabled();
    addr.blur();
    expect(await screen.findByText(ADDR_ERROR)).toBeInTheDocument();
    await userEvent.click(submit);
    expect(addCandidateLeader).not.toHaveBeenCalled();
  });
});

describe("AddWalletDialog — successful submit", () => {
  it("calls the action with the trimmed address + note, fires the success toast, and closes the Dialog", async () => {
    nextResult = { ok: true };
    render(<AddWalletDialog />);
    await userEvent.click(screen.getByRole("button", { name: BUTTON_LABEL }));
    await userEvent.type(screen.getByLabelText(ADDR_LABEL), `  ${VALID_ADDR}  `);
    await userEvent.type(screen.getByLabelText(NOTE_LABEL), "watching");
    await userEvent.click(screen.getByRole("button", { name: SUBMIT }));

    await waitFor(() => expect(addCandidateLeader).toHaveBeenCalledTimes(1));
    expect(addCandidateLeader).toHaveBeenCalledWith({
      address: VALID_ADDR,
      note: "watching",
    });
    expect(toastSuccess).toHaveBeenCalledWith(SUCCESS);
    expect(refresh).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("AddWalletDialog — duplicate response", () => {
  it("shows the '已在追蹤(目前狀態:<state>)' hint and keeps the Dialog open", async () => {
    nextResult = {
      ok: false,
      reason: "already_tracked",
      lifecycle_state: "paper",
    };
    render(<AddWalletDialog />);
    await userEvent.click(screen.getByRole("button", { name: BUTTON_LABEL }));
    await userEvent.type(screen.getByLabelText(ADDR_LABEL), VALID_ADDR);
    await userEvent.click(screen.getByRole("button", { name: SUBMIT }));

    expect(await screen.findByText(DUP_PAPER)).toBeInTheDocument();
    // Dialog stays open so the operator can dismiss / try another address.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(toastSuccess).not.toHaveBeenCalled();
    // Operator can dismiss via Cancel.
    await userEvent.click(screen.getByRole("button", { name: CANCEL }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
