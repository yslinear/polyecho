import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SidebarProvider } from "@/components/ui/sidebar";

// next/navigation is server-bound; mock usePathname so the client island
// can compute active state without a Next.js runtime.
const usePathname = vi.fn<() => string>(() => "/");
vi.mock("next/navigation", () => ({
  usePathname: () => usePathname(),
}));

// next/link -> plain anchor for jsdom. MUST spread the rest of props so the
// data-active attribute injected by the shadcn Sidebar <Slot> reaches the <a>.
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
  } & Record<string, unknown>) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

import { AppSidebar } from "@/components/app-sidebar";

function renderSidebar() {
  return render(
    <SidebarProvider>
      <AppSidebar />
    </SidebarProvider>
  );
}

const EXPECTED_NAV = [
  ["Dashboard", "/"],
  ["Leaders", "/leaders"],
  ["Discovery", "/discovery"],
  ["Backtest", "/backtest/new"],
  ["Trades", "/trades"],
  ["Positions", "/positions"],
  ["Accounts", "/accounts"],
  ["Settings", "/settings"],
] as const;

describe("AppSidebar", () => {
  beforeEach(() => {
    usePathname.mockReturnValue("/");
  });

  it("renders every nav item with the correct href", () => {
    renderSidebar();
    for (const [label, href] of EXPECTED_NAV) {
      const link = screen.getByRole("link", { name: label });
      expect(link).toHaveAttribute("href", href);
    }
  });

  it("renders the PolyEcho brand", () => {
    renderSidebar();
    expect(screen.getByText("PolyEcho")).toBeInTheDocument();
  });

  it("marks Dashboard active only on the exact root path", () => {
    usePathname.mockReturnValue("/");
    renderSidebar();
    const dashboard = screen
      .getByRole("link", { name: "Dashboard" })
      .closest("[data-active]");
    expect(dashboard).toHaveAttribute("data-active", "true");
  });

  it("does NOT keep Dashboard active on a non-root path", () => {
    usePathname.mockReturnValue("/discovery");
    renderSidebar();
    const dashboard = screen
      .getByRole("link", { name: "Dashboard" })
      .closest("[data-active]");
    expect(dashboard).toHaveAttribute("data-active", "false");
  });

  it("marks the section active for nested routes via startsWith", () => {
    usePathname.mockReturnValue("/discovery/0xabc123");
    renderSidebar();
    const discovery = screen
      .getByRole("link", { name: "Discovery" })
      .closest("[data-active]");
    expect(discovery).toHaveAttribute("data-active", "true");

    const leaders = screen
      .getByRole("link", { name: "Leaders" })
      .closest("[data-active]");
    expect(leaders).toHaveAttribute("data-active", "false");
  });

  it("marks Backtest active on the /backtest/new route", () => {
    usePathname.mockReturnValue("/backtest/new");
    renderSidebar();
    const backtest = screen
      .getByRole("link", { name: "Backtest" })
      .closest("[data-active]");
    expect(backtest).toHaveAttribute("data-active", "true");
  });

  it("does NOT highlight Backtest on /backtest/<id> detail routes (nav href is /backtest/new)", () => {
    // Documents current behaviour: the nav item key is "/backtest/new", so
    // startsWith("/backtest/new") is false for "/backtest/<uuid>". A backtest
    // result page therefore shows no active sidebar item. Tracked as a known
    // UX gap rather than asserted-correct.
    usePathname.mockReturnValue("/backtest/abc-123-uuid");
    renderSidebar();
    const backtest = screen
      .getByRole("link", { name: "Backtest" })
      .closest("[data-active]");
    expect(backtest).toHaveAttribute("data-active", "false");
  });
});
