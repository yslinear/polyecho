import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * /settings Server Component + ProxyForm island.
 *
 * The page reads the `polymarket_proxy_url` row through a mocked Drizzle
 * select chain (no DB). The form's server action and next/navigation +
 * sonner are mocked so nothing touches the network.
 */

// The page now reads several keys in ONE query:
//   db.select({...}).from(settings).where(inArray(key, [...]))
// so .where() resolves directly to the row array (no .limit()).
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

// PushSubscribe touches navigator/PushManager; stub it so the page test
// stays focused on the proxy/risk-mode reads (it has its own suite).
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
    const input = screen.getByLabelText("Proxy URL") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.getByText(/direct connection/i)).toBeInTheDocument();
  });

  it("reflects the stored risk_mode (conservative) in the toggle", async () => {
    rowsResult = [
      { key: "risk_mode", value: "conservative", updated_at: new Date() },
    ];
    render(await SettingsPage());
    expect(screen.getByRole("status")).toHaveTextContent(
      "保守 / 旅行模式"
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

  it("shows an inline error and does not call the action for socks5://", async () => {
    render(<ProxyForm current="" lastUpdated={null} />);
    const input = screen.getByLabelText("Proxy URL");
    await userEvent.type(input, "socks5://127.0.0.1:1080");
    await userEvent.click(screen.getByRole("button", { name: /save proxy/i }));
    expect(
      screen.getByText("SOCKS not supported; use http(s)")
    ).toBeInTheDocument();
    expect(setPolymarketProxy).not.toHaveBeenCalled();
  });

  it("submits a valid http URL, toasts success and refreshes", async () => {
    setPolymarketProxy.mockResolvedValue({
      ok: true,
      message: "Proxy saved. Takes effect on the next Polymarket call.",
      value: "http://10.0.0.1:8080",
    });
    render(<ProxyForm current="" lastUpdated={null} />);
    await userEvent.type(
      screen.getByLabelText("Proxy URL"),
      "http://10.0.0.1:8080"
    );
    await userEvent.click(screen.getByRole("button", { name: /save proxy/i }));
    expect(setPolymarketProxy).toHaveBeenCalledWith("http://10.0.0.1:8080");
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalled();
  });

  it("disables the primary action until the value changes", () => {
    render(<ProxyForm current="http://a.b:1" lastUpdated={null} />);
    expect(screen.getByRole("button", { name: /save proxy/i })).toBeDisabled();
  });
});
