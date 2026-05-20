import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * /settings Server Component + ProxyForm island.
 *
 * The page reads the `polymarket_proxy_url` row through a mocked Drizzle
 * select chain (no DB). The form's server action and next/navigation +
 * sonner are mocked so nothing touches the network.
 *
 * P7d / R24: setting a NEW non-empty proxy renders the InlineConfirm
 * primitive (CONFIRM_PHRASE only, no passphrase). Clearing stays single-click.
 */

let rowsResult: unknown[] = [];
const where = vi.fn(() => Promise.resolve(rowsResult));
const from = vi.fn(() => ({ where }));
const select = vi.fn(() => ({ from }));

vi.mock("@/lib/db", () => ({ db: { select: (...a: unknown[]) => select(...a) } }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const setPolymarketProxy = vi.fn();
const setRiskMode = vi.fn();
vi.mock("@/app/settings/actions", () => ({
  setPolymarketProxy: (...a: unknown[]) => setPolymarketProxy(...a),
  setRiskMode: (...a: unknown[]) => setRiskMode(...a),
}));

vi.mock("@/components/push-subscribe", () => ({
  PushSubscribe: () => null,
}));

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

import SettingsPage from "@/app/settings/page";
import { ProxyForm } from "@/app/settings/proxy-form";
import { t } from "@/lib/i18n";

const CONFIRM_PHRASE = "確認執行";
const PROXY_URL_LABEL = t("alertsSettings.settings.proxy.urlLabel");
const PROXY_SAVE = t("alertsSettings.settings.proxy.save");
const PROXY_ERR_SOCKS = t("alertsSettings.settings.proxy.errSocks");
const PROXY_DIRECT = t("alertsSettings.settings.proxy.currentDirect");
const IC_OPEN = t("inlineConfirm.openButton", { title: PROXY_SAVE });
const IC_SUBMIT = t("inlineConfirm.submitButton");
const IC_CONFIRM_LABEL = t("inlineConfirm.confirmPhraseLabel", {
  phrase: CONFIRM_PHRASE,
});

describe("/settings page (Server Component)", () => {
  beforeEach(() => {
    select.mockClear();
    rowsResult = [];
  });

  it("renders the current proxy value when a bare-string row exists", async () => {
    rowsResult = [
      {
        key: "polymarket_proxy_url",
        value: "http://127.0.0.1:8080",
        updated_at: new Date("2026-05-18T10:00:00Z"),
      },
    ];
    render(await SettingsPage());
    expect(screen.getByDisplayValue("http://127.0.0.1:8080")).toBeInTheDocument();
  });

  it("normalises the {value:...} object shape to a plain string", async () => {
    rowsResult = [
      {
        key: "polymarket_proxy_url",
        value: { value: "https://proxy.local:3128" },
        updated_at: new Date(),
      },
    ];
    render(await SettingsPage());
    expect(
      screen.getByDisplayValue("https://proxy.local:3128")
    ).toBeInTheDocument();
  });

  it("renders an empty input and 'direct connection' when no row exists", async () => {
    rowsResult = [];
    render(await SettingsPage());
    const input = screen.getByLabelText(PROXY_URL_LABEL) as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.getByText(PROXY_DIRECT)).toBeInTheDocument();
  });

  it("reflects the stored risk_mode (conservative) in the toggle", async () => {
    rowsResult = [
      { key: "risk_mode", value: "conservative", updated_at: new Date() },
    ];
    render(await SettingsPage());
    expect(screen.getByRole("status")).toHaveTextContent(
      t("alertsSettings.settings.riskMode.conservative")
    );
  });
});

describe("ProxyForm (client island)", () => {
  beforeEach(() => {
    setPolymarketProxy.mockReset();
    refresh.mockClear();
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  it("shows an inline error for socks5:// on blur (no action call)", async () => {
    render(<ProxyForm current="" lastUpdated={null} />);
    const input = screen.getByLabelText(PROXY_URL_LABEL);
    await userEvent.type(input, "socks5://127.0.0.1:1080");
    // Tab away from the input to trigger the on-blur validate.
    await userEvent.tab();
    expect(await screen.findByText(PROXY_ERR_SOCKS)).toBeInTheDocument();
    expect(setPolymarketProxy).not.toHaveBeenCalled();
  });

  it("submits a valid http URL through the inline-confirm gate", async () => {
    setPolymarketProxy.mockResolvedValue({
      ok: true,
      message: "Proxy saved. Takes effect on the next Polymarket call.",
      value: "http://10.0.0.1:8080",
    });
    render(<ProxyForm current="" lastUpdated={null} />);
    await userEvent.type(
      screen.getByLabelText(PROXY_URL_LABEL),
      "http://10.0.0.1:8080"
    );
    // The inline-confirm trigger replaces the bare submit button.
    await userEvent.click(
      screen.getByRole("button", { name: new RegExp(PROXY_SAVE) })
    );
    await userEvent.type(
      screen.getByLabelText(IC_CONFIRM_LABEL),
      CONFIRM_PHRASE
    );
    await userEvent.click(screen.getByRole("button", { name: IC_SUBMIT }));
    await waitFor(() =>
      expect(setPolymarketProxy).toHaveBeenCalledWith({
        value: "http://10.0.0.1:8080",
        confirmText: CONFIRM_PHRASE,
      })
    );
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalled();
  });

  it("clearing (empty value) stays single-click (safe-direction)", async () => {
    setPolymarketProxy.mockResolvedValue({
      ok: true,
      message: "Proxy cleared. Polymarket calls now go direct.",
      value: "",
    });
    render(<ProxyForm current="http://a.b:1" lastUpdated={null} />);
    const input = screen.getByLabelText(PROXY_URL_LABEL);
    await userEvent.clear(input);
    await userEvent.click(
      screen.getByRole("button", { name: PROXY_SAVE })
    );
    expect(setPolymarketProxy).toHaveBeenCalledWith("");
    expect(toastSuccess).toHaveBeenCalled();
  });

  it("hides the inline-confirm trigger when value matches current and not dirty", () => {
    render(<ProxyForm current="" lastUpdated={null} />);
    // No InlineConfirm trigger should be present at empty/initial state
    // (the safe-direction clear button is disabled until dirty).
    expect(
      screen.queryByRole("button", { name: IC_OPEN })
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: PROXY_SAVE })).toBeDisabled();
  });
});
