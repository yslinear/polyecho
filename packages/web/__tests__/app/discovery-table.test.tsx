import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// discovery-table imports a *type* from ./page, but page.tsx also imports
// "@/lib/db" at module load (which throws without POLYECHO_DB_URL_DRIZZLE).
// Stub the db + env modules so importing the island stays DB-free.
vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

import { DiscoveryTable } from "@/app/discovery/discovery-table";
import type { CandidateLeaderRow } from "@/app/discovery/page";

function makeRow(p: Partial<CandidateLeaderRow>): CandidateLeaderRow {
  return {
    wallet_address: "0x0000000000000000000000000000000000000000",
    display_name: null,
    sources: null,
    first_discovered_at: null,
    last_active_at: null,
    stats_7d: null,
    stats_30d: null,
    stats_90d: null,
    frequency_stats: null,
    strategy_type: null,
    backtest_default: null,
    profit_score: null,
    replicability_score: null,
    total_score: null,
    risk_flags: null,
    specialization: null,
    user_status: "new",
    user_notes: null,
    updated_at: new Date(),
    ...p,
  } as CandidateLeaderRow;
}

const rows: CandidateLeaderRow[] = [
  makeRow({
    wallet_address: "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    display_name: "Alpha",
    strategy_type: "Scalper",
    total_score: "80",
    user_status: "following",
    risk_flags: ["wash_trading"],
  }),
  makeRow({
    wallet_address: "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    display_name: "Beta",
    strategy_type: "Swing",
    total_score: "40",
    user_status: "new",
    risk_flags: [],
  }),
  makeRow({
    wallet_address: "0xcccccccccccccccccccccccccccccccccccccccc",
    display_name: "Gamma",
    strategy_type: "Scalper",
    total_score: "60",
    user_status: "new",
    risk_flags: null,
  }),
];

function bodyRows() {
  return screen.getAllByRole("row").slice(1);
}

describe("DiscoveryTable (client island)", () => {
  it("renders one row per leader with shortened wallet links", () => {
    render(<DiscoveryTable data={rows} />);
    expect(bodyRows()).toHaveLength(3);
    // shortWallet => 0xaaaa…aaaa
    const link = screen.getByRole("link", { name: "0xaaaa…aaaa" });
    expect(link).toHaveAttribute(
      "href",
      "/discovery/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    );
  });

  it("renders a destructive badge for each risk flag", () => {
    render(<DiscoveryTable data={rows} />);
    expect(screen.getByText("wash_trading")).toBeInTheDocument();
  });

  it("sorts by Total score ascending then descending", async () => {
    render(<DiscoveryTable data={rows} />);
    const totalHeader = screen.getByRole("button", { name: /Total/ });

    await userEvent.click(totalHeader); // asc
    let names = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[1].textContent
    );
    expect(names).toEqual(["Beta", "Gamma", "Alpha"]);

    await userEvent.click(totalHeader); // desc
    names = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[1].textContent
    );
    expect(names).toEqual(["Alpha", "Gamma", "Beta"]);
  });

  it("text-filters by wallet address", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.type(
      screen.getByPlaceholderText("Filter by wallet…"),
      "0xbbbb"
    );
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("Beta")).toBeInTheDocument();
  });

  it("faceted Status filter narrows to a single status", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(screen.getByRole("button", { name: /Status/ }));
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: "Following" })
    );
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("faceted Strategy filter narrows to matching strategy", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(screen.getByRole("button", { name: /Strategy/ }));
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: "Scalper" })
    );
    // Alpha + Gamma are Scalpers
    expect(bodyRows()).toHaveLength(2);
  });

  it("Risk Flag faceted filter splits has-flags vs no-flags", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(
      screen.getByRole("button", { name: /Risk Flag/ })
    );
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: "Has flags" })
    );
    expect(bodyRows()).toHaveLength(1); // only Alpha has flags
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });
});
