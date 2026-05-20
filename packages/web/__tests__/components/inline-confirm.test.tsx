import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * InlineConfirm primitive (P7d / R24).
 *
 * Asserts the BEHAVIOUR contract shared by every risk-increasing Server
 * Action in P7d:
 *   - collapsed by default (no modal-first)
 *   - expands inline on trigger click
 *   - disabled submit until both gates match the requirement (confirmText
 *     must equal CONFIRM_PHRASE; passphrase non-empty when required)
 *   - calls the bound action with the typed gate inputs
 *   - on `{ok:true}` toasts, refreshes, collapses + resets
 *   - on `{ok:false}` toasts the server message and clears the gate inputs
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { InlineConfirm } from "@/components/inline-confirm";
import { t } from "@/lib/i18n";

const CONFIRM_PHRASE = "確認執行";
const TITLE = "恢復全部跟單";
const OPEN = t("inlineConfirm.openButton", { title: TITLE });
const SUBMIT = t("inlineConfirm.submitButton");
const CONFIRM_LABEL = t("inlineConfirm.confirmPhraseLabel", {
  phrase: CONFIRM_PHRASE,
});
const PASS_LABEL = t("inlineConfirm.passphraseLabel");

beforeEach(() => {
  refresh.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("InlineConfirm (collapsed by default)", () => {
  it("renders only a collapsed trigger initially", () => {
    const action = vi.fn(async () => ({ ok: true, message: "OK" }));
    render(<InlineConfirm action={action} title={TITLE} />);
    expect(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    ).toBeInTheDocument();
    // No confirm phrase input until expanded.
    expect(screen.queryByLabelText(CONFIRM_LABEL)).not.toBeInTheDocument();
  });

  it("expands inline when the trigger is clicked (no modal)", async () => {
    const action = vi.fn(async () => ({ ok: true, message: "OK" }));
    render(<InlineConfirm action={action} title={TITLE} />);
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    );
    expect(screen.getByLabelText(CONFIRM_LABEL)).toBeInTheDocument();
    expect(
      screen.getByRole("form", {
        name: t("inlineConfirm.formAria", { title: TITLE }),
      })
    ).toBeInTheDocument();
  });
});

describe("InlineConfirm gate behaviour", () => {
  it("disables submit until the verbatim confirm phrase is typed (no-passphrase mode)", async () => {
    const action = vi.fn(async () => ({ ok: true, message: "OK" }));
    render(<InlineConfirm action={action} title={TITLE} />);
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    );
    const submit = screen.getByRole("button", { name: SUBMIT });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), "wrong");
    expect(submit).toBeDisabled();
    await userEvent.clear(screen.getByLabelText(CONFIRM_LABEL));
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRM_PHRASE);
    expect(submit).not.toBeDisabled();
  });

  it("requires both confirm phrase AND passphrase when requirePassphrase=true", async () => {
    const action = vi.fn(async () => ({ ok: true, message: "OK" }));
    render(
      <InlineConfirm action={action} title={TITLE} requirePassphrase={true} />
    );
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    );
    const submit = screen.getByRole("button", { name: SUBMIT });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRM_PHRASE);
    // Still disabled — passphrase missing.
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText(PASS_LABEL), "secret");
    expect(submit).not.toBeDisabled();
  });

  it("does not render a passphrase input by default", async () => {
    const action = vi.fn(async () => ({ ok: true, message: "OK" }));
    render(<InlineConfirm action={action} title={TITLE} />);
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    );
    expect(screen.queryByLabelText(PASS_LABEL)).not.toBeInTheDocument();
  });

  it("calls the bound action with the typed inputs on submit", async () => {
    const action = vi.fn(async () => ({ ok: true, message: "OK" }));
    render(
      <InlineConfirm
        action={action}
        title={TITLE}
        requirePassphrase={true}
        successToast="resumed"
      />
    );
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    );
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRM_PHRASE);
    await userEvent.type(screen.getByLabelText(PASS_LABEL), "operator-secret");
    await userEvent.click(screen.getByRole("button", { name: SUBMIT }));
    await waitFor(() =>
      expect(action).toHaveBeenCalledWith({
        confirmText: CONFIRM_PHRASE,
        passphrase: "operator-secret",
      })
    );
    expect(toastSuccess).toHaveBeenCalledWith("resumed");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("toasts the server message and clears gate inputs on {ok:false}", async () => {
    const action = vi.fn(async () => ({
      ok: false,
      message: "通行碼錯誤，未執行任何操作。請重試。",
    }));
    render(
      <InlineConfirm
        action={action}
        title={TITLE}
        requirePassphrase={true}
      />
    );
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    );
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRM_PHRASE);
    await userEvent.type(screen.getByLabelText(PASS_LABEL), "wrong");
    await userEvent.click(screen.getByRole("button", { name: SUBMIT }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    // The form stays open; both gate inputs were cleared so the operator can
    // retry without rebuilding state.
    expect(screen.getByLabelText(CONFIRM_LABEL)).toHaveValue("");
    expect(screen.getByLabelText(PASS_LABEL)).toHaveValue("");
    expect(refresh).not.toHaveBeenCalled();
  });

  it("collapses on cancel and discards both gate inputs", async () => {
    const action = vi.fn(async () => ({ ok: true, message: "OK" }));
    render(
      <InlineConfirm
        action={action}
        title={TITLE}
        requirePassphrase={true}
      />
    );
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(TITLE) })
    );
    await userEvent.type(screen.getByLabelText(CONFIRM_LABEL), CONFIRM_PHRASE);
    await userEvent.click(
      screen.getByRole("button", { name: t("inlineConfirm.cancelButton") })
    );
    expect(screen.queryByLabelText(CONFIRM_LABEL)).not.toBeInTheDocument();
    expect(action).not.toHaveBeenCalled();
  });

  it("i18n: every used inline-confirm key resolves to a non-dotted string", () => {
    // The i18n module returns the dotted key itself on a miss. A non-equal
    // result proves each key is actually present in zh-TW.
    const keys = [
      "inlineConfirm.openButton",
      "inlineConfirm.openAria",
      "inlineConfirm.formAria",
      "inlineConfirm.sectionTitle",
      "inlineConfirm.intro",
      "inlineConfirm.confirmPhraseLabel",
      "inlineConfirm.confirmPhrasePlaceholder",
      "inlineConfirm.passphraseLabel",
      "inlineConfirm.passphrasePlaceholder",
      "inlineConfirm.passphraseHint",
      "inlineConfirm.submitButton",
      "inlineConfirm.submitting",
      "inlineConfirm.cancelButton",
      "inlineConfirm.submitAria",
      "inlineConfirm.errConfirmPhrase",
      "inlineConfirm.errPassphrase",
      "inlineConfirm.errSubmitFailed",
    ] as const;
    for (const k of keys) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const out = t(k as any, { phrase: CONFIRM_PHRASE, title: TITLE });
      expect(out).not.toBe(k);
      expect(typeof out).toBe("string");
      expect(out.length).toBeGreaterThan(0);
    }
  });
});
