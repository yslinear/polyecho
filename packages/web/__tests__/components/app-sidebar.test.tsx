import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SidebarProvider } from "@/components/ui/sidebar";
import { t } from "@/lib/i18n";

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

// Locators are derived from the i18n dictionary, not hardcoded text: the
// test asserts the sidebar STRUCTURE (label↔href, active state), and stays
// correct if a translation string changes — while still proving the i18n
// seam renders an accessible name for every item.
const DASHBOARD = t("sidebar.items.dashboard");
const LEADERS = t("sidebar.items.leaders");
const DISCOVERY = t("sidebar.items.discovery");
const BACKTEST = t("sidebar.items.backtest");

const EXPECTED_NAV = [
  [DASHBOARD, "/"],
  [LEADERS, "/leaders"],
  [DISCOVERY, "/discovery"],
  [BACKTEST, "/backtest/new"],
  [t("sidebar.items.trades"), "/trades"],
  [t("sidebar.items.positions"), "/positions"],
  [t("sidebar.items.accounts"), "/accounts"],
  [t("sidebar.items.alerts"), "/alerts"],
  [t("sidebar.items.status"), "/status"],
  [t("sidebar.items.settings"), "/settings"],
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
      .getByRole("link", { name: DASHBOARD })
      .closest("[data-active]");
    expect(dashboard).toHaveAttribute("data-active", "true");
  });

  it("does NOT keep Dashboard active on a non-root path", () => {
    usePathname.mockReturnValue("/discovery");
    renderSidebar();
    const dashboard = screen
      .getByRole("link", { name: DASHBOARD })
      .closest("[data-active]");
    expect(dashboard).toHaveAttribute("data-active", "false");
  });

  it("marks the section active for nested discovery routes", () => {
    usePathname.mockReturnValue("/discovery/0xabc123");
    renderSidebar();
    const discovery = screen
      .getByRole("link", { name: DISCOVERY })
      .closest("[data-active]");
    expect(discovery).toHaveAttribute("data-active", "true");

    const leaders = screen
      .getByRole("link", { name: LEADERS })
      .closest("[data-active]");
    expect(leaders).toHaveAttribute("data-active", "false");
  });

  it("marks Backtest active on the /backtest/new route", () => {
    usePathname.mockReturnValue("/backtest/new");
    renderSidebar();
    const backtest = screen
      .getByRole("link", { name: BACKTEST })
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
      .getByRole("link", { name: BACKTEST })
      .closest("[data-active]");
    expect(backtest).toHaveAttribute("data-active", "true");
  });

  // Exactly one item is active for each canonical route, and it is the
  // correct one. Guards against the "/" matching everything and against
  // sibling sections leaking into each other.
  const ACTIVE_MATRIX: Array<[string, string]> = [
    ["/", DASHBOARD],
    ["/discovery", DISCOVERY],
    ["/discovery/0xabc", DISCOVERY],
    ["/backtest/new", BACKTEST],
    ["/backtest/abc-123-uuid", BACKTEST],
    ["/trades", t("sidebar.items.trades")],
    ["/positions", t("sidebar.items.positions")],
    ["/accounts", t("sidebar.items.accounts")],
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
