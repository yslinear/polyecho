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
  ["Alerts", "/alerts"],
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

  it("marks the section active for nested discovery routes", () => {
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

  it("highlights Backtest on /backtest/<id> detail routes (section match)", () => {
    // Regression guard for the p3-dashboard UX gap: the nav href is
    // "/backtest/new" but the section is "/backtest", so a backtest result
    // page MUST still highlight the Backtest item.
    usePathname.mockReturnValue("/backtest/abc-123-uuid");
    renderSidebar();
    const backtest = screen
      .getByRole("link", { name: "Backtest" })
      .closest("[data-active]");
    expect(backtest).toHaveAttribute("data-active", "true");
  });

  // Exactly one item is active for each canonical route, and it is the
  // correct one. Guards against the "/" matching everything and against
  // sibling sections leaking into each other.
  const ACTIVE_MATRIX: Array<[string, string]> = [
    ["/", "Dashboard"],
    ["/discovery", "Discovery"],
    ["/discovery/0xabc", "Discovery"],
    ["/backtest/new", "Backtest"],
    ["/backtest/abc-123-uuid", "Backtest"],
    ["/trades", "Trades"],
    ["/positions", "Positions"],
    ["/accounts", "Accounts"],
  ];

  it.each(ACTIVE_MATRIX)(
    "on %s exactly one item is active and it is %s",
    (path, expectedLabel) => {
      usePathname.mockReturnValue(path);
      renderSidebar();

      const activeLabels: string[] = [];
      for (const [label] of EXPECTED_NAV) {
        const el = screen
          .getByRole("link", { name: label })
          .closest("[data-active]");
        if (el?.getAttribute("data-active") === "true") {
          activeLabels.push(label);
        }
      }

      expect(activeLabels).toEqual([expectedLabel]);
    }
  );
});
