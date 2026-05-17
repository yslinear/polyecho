import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// discovery-table imports a *type* from ./page, but page.tsx also imports
// "@/lib/db" at module load (which throws without POLYECHO_DB_URL_DRIZZLE).
// Stub db + env so importing the island stays DB-free.
vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

// next/navigation is server-bound; mock useRouter so row navigation works.
const push = vi.fn<(href: string) => void>();
const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
}));

// Server Actions are DB-bound; mock the module so the island stays DB-free.
const quickBacktest = vi.fn(async () => ({ id: "job-123" }));
const updateLeaderStatus = vi.fn(
  async (wallet: string, status: string) => ({ wallet, status })
);
vi.mock("@/app/discovery/actions", () => ({
  quickBacktest: (...a: unknown[]) => quickBacktest(...a),
  updateLeaderStatus: (...a: unknown[]) => updateLeaderStatus(...a),
}));

// sonner toast — assert messages without a real Toaster.
const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
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
    profit_score: "75",
    user_status: "following",
    risk_flags: ["insider_suspect"],
  }),
  makeRow({
    wallet_address: "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    display_name: "Beta",
    strategy_type: "Swing",
    total_score: "40",
    profit_score: "30",
    user_status: "new",
    risk_flags: [],
  }),
  makeRow({
    wallet_address: "0xcccccccccccccccccccccccccccccccccccccccc",
    display_name: "Gamma",
    strategy_type: "Scalper",
    total_score: "60",
    profit_score: "55",
    user_status: "new",
    risk_flags: null,
  }),
];

function bodyRows() {
  // Data rows carry an aria-label ("View leader 0x…"); the header row and the
  // empty-state row do not, so this counts real leader rows only.
  return screen
    .queryAllByRole("row")
    .filter((r) => r.getAttribute("aria-label")?.startsWith("View leader"));
}

function names() {
  return bodyRows().map(
    (r) => within(r).getAllByRole("cell")[1].textContent
  );
}

beforeEach(() => {
  push.mockClear();
  refresh.mockClear();
  quickBacktest.mockClear();
  updateLeaderStatus.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("DiscoveryTable (shadcn data-table)", () => {
  it("wraps the table in a Card titled 'Candidate Leaders' with a total-count badge", () => {
    render(<DiscoveryTable data={rows} />);
    expect(screen.getByText("Candidate Leaders")).toBeInTheDocument();
    // count badge shows total dataset size (3)
    const title = screen.getByText("Candidate Leaders");
    expect(within(title).getByText("3")).toBeInTheDocument();
  });

  it("renders one row per leader with shortened monospace wallet", () => {
    render(<DiscoveryTable data={rows} />);
    expect(bodyRows()).toHaveLength(3);
    expect(screen.getByText("0xaaaa…aaaa")).toBeInTheDocument();
  });

  it("defaults to sorting by Total score descending", () => {
    render(<DiscoveryTable data={rows} />);
    expect(names()).toEqual(["Alpha", "Gamma", "Beta"]);
  });

  it("toggles aria-sort on the Total header th when sorting", async () => {
    render(<DiscoveryTable data={rows} />);
    const totalBtn = screen.getByRole("button", { name: /Total/ });
    const th = totalBtn.closest("th")!;

    // default state = descending
    expect(th).toHaveAttribute("aria-sort", "descending");

    await userEvent.click(totalBtn); // -> ascending
    expect(th).toHaveAttribute("aria-sort", "ascending");
    expect(names()).toEqual(["Beta", "Gamma", "Alpha"]);

    await userEvent.click(totalBtn); // -> descending
    expect(th).toHaveAttribute("aria-sort", "descending");
    expect(names()).toEqual(["Alpha", "Gamma", "Beta"]);
  });

  it("marks non-sorted sortable headers aria-sort='none' and unsortable headers without it", () => {
    render(<DiscoveryTable data={rows} />);
    const profitTh = screen
      .getByRole("button", { name: /Profit/ })
      .closest("th")!;
    expect(profitTh).toHaveAttribute("aria-sort", "none");

    // Wallet is not sortable -> no aria-sort
    const walletTh = screen.getByText("Wallet").closest("th")!;
    expect(walletTh).not.toHaveAttribute("aria-sort");
  });

  it("global search narrows by wallet or display name", async () => {
    render(<DiscoveryTable data={rows} />);
    const search = screen.getByPlaceholderText("Search wallet or name…");

    await userEvent.type(search, "beta");
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("Beta")).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, "0xcccc");
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("Gamma")).toBeInTheDocument();
  });

  it("faceted Strategy filter narrows rows", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(screen.getByRole("button", { name: /Strategy/ }));
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: "Scalper" })
    );
    // Alpha + Gamma are Scalpers
    expect(bodyRows()).toHaveLength(2);
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

  it("Risk Flag faceted filter splits has-flags vs no-flags", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(screen.getByRole("button", { name: /Risk Flag/ }));
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: "Has risk flag" })
    );
    expect(bodyRows()).toHaveLength(1); // only Alpha has flags
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("shows a Reset button only when filtered and clears all filters", async () => {
    render(<DiscoveryTable data={rows} />);
    expect(
      screen.queryByRole("button", { name: /Reset/ })
    ).not.toBeInTheDocument();

    await userEvent.type(
      screen.getByPlaceholderText("Search wallet or name…"),
      "beta"
    );
    expect(bodyRows()).toHaveLength(1);

    const reset = screen.getByRole("button", { name: /Reset/ });
    await userEvent.click(reset);
    expect(bodyRows()).toHaveLength(3);
    expect(
      screen.queryByRole("button", { name: /Reset/ })
    ).not.toBeInTheDocument();
  });

  it("column-visibility menu hides a column", async () => {
    render(<DiscoveryTable data={rows} />);
    const replicabilityHeader = () =>
      screen.queryByRole("button", { name: /Replicability/ });
    expect(replicabilityHeader()).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Columns/ }));
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: "Replicability" })
    );
    // sortable header button for Replicability is gone
    expect(replicabilityHeader()).not.toBeInTheDocument();
  });

  it("renders strategy, status and risk badges (insider_suspect = destructive)", () => {
    render(<DiscoveryTable data={rows} />);
    // Alpha + Gamma are both Scalper -> two strategy badges
    expect(screen.getAllByText("Scalper").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Swing")).toBeInTheDocument();
    expect(screen.getByText("following")).toBeInTheDocument();
    const riskBadge = screen.getByText("insider_suspect");
    expect(riskBadge.className).toMatch(/destructive/);
  });

  it("shows a calm 'None' for rows with no risk flags", () => {
    render(<DiscoveryTable data={rows} />);
    // Beta + Gamma have no flags -> at least two "None" cells. (The only
    // em dash left on the page comes from the frozen shared ScoreBadge for
    // null scores — not from this owned table's own copy.)
    expect(screen.getAllByText("None").length).toBeGreaterThanOrEqual(2);
  });

  it("uses 'Not set' (not an em dash) for missing name/strategy", () => {
    render(<DiscoveryTable data={[makeRow({ wallet_address: "0x1" })]} />);
    // display_name + strategy_type are null on this row.
    expect(screen.getAllByText("Not set").length).toBeGreaterThanOrEqual(2);
  });

  it("paginates: page size Select changes page and Next advances", async () => {
    const many: CandidateLeaderRow[] = Array.from({ length: 14 }, (_, i) =>
      makeRow({
        wallet_address: `0x${String(i).padStart(40, "0")}`,
        display_name: `User${i}`,
        total_score: String(i),
      })
    );
    render(<DiscoveryTable data={many} />);

    // open the rows-per-page Select and pick 10
    await userEvent.click(screen.getByRole("combobox", { name: /Rows per page/ }));
    await userEvent.click(await screen.findByRole("option", { name: "10" }));

    expect(bodyRows()).toHaveLength(10);
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
    expect(screen.getByText("1–10 of 14")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("Page 2 of 2")).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(4);
    expect(screen.getByText("11–14 of 14")).toBeInTheDocument();
  });

  it("navigates to the leader detail route on row click", async () => {
    render(<DiscoveryTable data={rows} />);
    // first body row (default sort desc -> Alpha)
    await userEvent.click(bodyRows()[0]);
    expect(push).toHaveBeenCalledWith(
      "/discovery/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    );
  });

  it("navigates on keyboard Enter for a focused row", async () => {
    render(<DiscoveryTable data={rows} />);
    const row = bodyRows()[0];
    row.focus();
    await userEvent.keyboard("{Enter}");
    expect(push).toHaveBeenCalledWith(
      "/discovery/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    );
  });

  it("shows a calm filtered empty state with a real Clear-filters action", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.type(
      screen.getByPlaceholderText("Search wallet or name…"),
      "zzzznomatch"
    );
    expect(bodyRows()).toHaveLength(0);
    expect(
      screen.getByText("No leaders match these filters.")
    ).toBeInTheDocument();
    // The empty state offers the real next action, and clicking it recovers.
    const clear = screen.getByRole("button", { name: /Clear filters/ });
    await userEvent.click(clear);
    expect(bodyRows()).toHaveLength(3);
  });

  it("shows the calm 'No candidates yet. Run discovery.' state with no data", () => {
    render(<DiscoveryTable data={[]} />);
    // DESIGN-mandated single calm sentence + the real next action.
    expect(
      screen.getByText("No candidates yet. Run discovery.")
    ).toBeInTheDocument();
    expect(
      screen.getByText(/The pool fills once the discovery scan finds/)
    ).toBeInTheDocument();
  });
});

describe("DiscoveryTable per-row quick actions", () => {
  const ALPHA = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const BETA = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

  function actionsTrigger(wallet: string) {
    return screen.getByRole("button", { name: `Actions for ${wallet}` });
  }

  it("opens the per-row actions menu", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(ALPHA));
    expect(
      await screen.findByRole("menuitem", {
        name: /Quick backtest \(90d · Balanced\)/,
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: /Customize backtest/ })
    ).toBeInTheDocument();
  });

  it("quick backtest calls the action then toasts + navigates", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(BETA));
    await userEvent.click(
      await screen.findByRole("menuitem", {
        name: /Quick backtest \(90d · Balanced\)/,
      })
    );

    expect(quickBacktest).toHaveBeenCalledTimes(1);
    expect(quickBacktest).toHaveBeenCalledWith(BETA);
    expect(toastSuccess).toHaveBeenCalledWith(
      "Backtest queued (90d · Balanced)"
    );
    expect(push).toHaveBeenCalledWith("/backtest/job-123");
  });

  it("customize backtest navigates to /backtest/new with the leader query", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(BETA));
    await userEvent.click(
      await screen.findByRole("menuitem", { name: /Customize backtest/ })
    );
    expect(push).toHaveBeenCalledWith(`/backtest/new?leader=${BETA}`);
    expect(quickBacktest).not.toHaveBeenCalled();
  });

  it("status items call updateLeaderStatus with the right status + refresh", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(BETA));
    await userEvent.click(
      await screen.findByRole("menuitem", { name: "Follow" })
    );
    expect(updateLeaderStatus).toHaveBeenCalledWith(BETA, "following");
    expect(toastSuccess).toHaveBeenCalledWith("Leader marked follow");
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(push).not.toHaveBeenCalled();
  });

  it("hides the menu item matching the leader's current status", async () => {
    render(<DiscoveryTable data={rows} />);
    // Alpha.user_status === "following" -> no "Follow" item, Watch/Reject present
    await userEvent.click(actionsTrigger(ALPHA));
    expect(
      await screen.findByRole("menuitem", { name: "Watch" })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "Reject" })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: "Follow" })
    ).not.toBeInTheDocument();
  });

  it("clicking the actions trigger does NOT navigate to the row detail", async () => {
    render(<DiscoveryTable data={rows} />);
    await userEvent.click(actionsTrigger(ALPHA));
    // menu opened, but row→detail push must not have fired
    expect(push).not.toHaveBeenCalled();
  });

  it("row click still navigates while action area swallows clicks", async () => {
    render(<DiscoveryTable data={rows} />);
    // click a non-action cell of the first row (default sort -> Alpha)
    const firstRow = bodyRows()[0];
    await userEvent.click(within(firstRow).getAllByRole("cell")[1]);
    expect(push).toHaveBeenCalledWith(`/discovery/${ALPHA}`);
    expect(quickBacktest).not.toHaveBeenCalled();
    expect(updateLeaderStatus).not.toHaveBeenCalled();
  });
});
