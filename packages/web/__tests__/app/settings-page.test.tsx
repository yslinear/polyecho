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

const limit = vi.fn();
const where = vi.fn(() => ({ limit }));
const from = vi.fn(() => ({ where }));
const select = vi.fn(() => ({ from }));

vi.mock("@/lib/db", () => ({ db: { select: (...a: unknown[]) => select(...a) } }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const setPolymarketProxy = vi.fn();
vi.mock("@/app/settings/actions", () => ({
  setPolymarketProxy: (...a: unknown[]) => setPolymarketProxy(...a),
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
    limit.mockReset();
  });

  it("renders the current proxy value when a bare-string row exists", async () => {
    limit.mockResolvedValue([
      { value: "http://127.0.0.1:8080", updated_at: new Date("2026-05-18T10:00:00Z") },
    ]);
    render(await SettingsPage());
    expect(screen.getByDisplayValue("http://127.0.0.1:8080")).toBeInTheDocument();
  });

  it("normalises the {value:...} object shape to a plain string", async () => {
    limit.mockResolvedValue([
      { value: { value: "https://proxy.local:3128" }, updated_at: new Date() },
    ]);
    render(await SettingsPage());
    expect(
      screen.getByDisplayValue("https://proxy.local:3128")
    ).toBeInTheDocument();
  });

  it("renders an empty input and 'direct connection' when no row exists", async () => {
    limit.mockResolvedValue([]);
    render(await SettingsPage());
    const input = screen.getByLabelText("Proxy URL") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.getByText(/direct connection/i)).toBeInTheDocument();
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
