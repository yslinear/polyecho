import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { t } from "@/lib/i18n";

// trades-table imports a *type* from ./page, and page.tsx imports "@/lib/db"
// at module load (throws without POLYECHO_DB_URL_DRIZZLE). Stub db + env.
vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import { TradesTable } from "@/app/trades/trades-table";
import type { MyTradeRow } from "@/app/trades/page";

function makeRow(p: Partial<MyTradeRow>): MyTradeRow {
  return {
    id: 1,
    account_id: "00000000-0000-0000-0000-000000000000",
    leader_address: "0xabcdef0123456789abcdef0123456789abcdef01",
    copy_of_trade_id: null,
    market_id: "market-A",
    side: "BUY",
    size: "100",
    price: "0.5",
    fill_status: "filled",
    pnl_usd: "12.34",
    opened_at: new Date("2026-05-17T01:00:00.000Z"),
    closed_at: null,
    ...p,
  } as MyTradeRow;
}

const rows: MyTradeRow[] = [
  makeRow({
    id: 1,
    market_id: "market-A",
    fill_status: "filled",
    pnl_usd: "12.34",
    size: "100",
    side: "BUY",
    opened_at: new Date("2026-05-17T03:00:00.000Z"),
  }),
  makeRow({
    id: 2,
    market_id: "market-B",
    fill_status: "pending",
    pnl_usd: "-5",
    size: "300",
    side: "SELL",
    opened_at: new Date("2026-05-17T01:00:00.000Z"),
  }),
  makeRow({
    id: 3,
    market_id: "market-C",
    fill_status: "rejected",
    pnl_usd: null,
    size: "200",
    side: "BUY",
    opened_at: new Date("2026-05-17T02:00:00.000Z"),
  }),
];

function bodyRows() {
  return screen.getAllByRole("row").slice(1);
}

describe("TradesTable (client island)", () => {
  it("renders one row per trade with formatted PnL and fill badges", () => {
    render(<TradesTable data={rows} />);
    expect(bodyRows()).toHaveLength(3);
    // Money + fill_status value are language-neutral data (rendered raw).
    expect(screen.getByText("+$12.34")).toBeInTheDocument();
    expect(screen.getByText("-$5.00")).toBeInTheDocument();
    expect(screen.getByText("filled")).toBeInTheDocument();
    expect(screen.getByText("rejected")).toBeInTheDocument();
  });

  it("shortens the leader address", () => {
    render(<TradesTable data={rows} />);
    expect(screen.getAllByText("0xabcd…ef01").length).toBeGreaterThan(0);
  });

  it("does NOT duplicate the page empty state — the parent owns it", () => {
    render(<TradesTable data={[]} />);
    // Single-owner contract (mirrors /positions): the genuinely-empty
    // dataset copy (tables.trades.emptyTitle) belongs to the parent Server
    // Component, NOT this island. The island only shows the DataTable's
    // own "no matches" fallback for the filter-excludes-everything case.
    expect(
      screen.queryByText(t("tables.trades.emptyTitle"))
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(t("common.dataTable.empty"))
    ).toBeInTheDocument();
  });

  it("sorts by Size ascending then descending", async () => {
    render(<TradesTable data={rows} />);
    const sizeHeader = screen.getByRole("button", {
      name: new RegExp(t("tables.trades.col.size")),
    });

    await userEvent.click(sizeHeader); // asc
    let sizes = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[4].textContent
    );
    expect(sizes).toEqual(["100", "200", "300"]);

    await userEvent.click(sizeHeader); // desc
    sizes = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[4].textContent
    );
    expect(sizes).toEqual(["300", "200", "100"]);
  });

  it("faceted Fill Status filter narrows to a single status", async () => {
    render(<TradesTable data={rows} />);
    await userEvent.click(
      screen.getByRole("button", {
        name: new RegExp(t("tables.trades.filter.fillStatus")),
      })
    );
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", {
        name: t("tables.trades.filter.pending"),
      })
    );
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("market-B")).toBeInTheDocument();
  });

  it("free-text filters by market id", async () => {
    render(<TradesTable data={rows} />);
    await userEvent.type(
      screen.getByPlaceholderText(t("tables.trades.searchPlaceholder")),
      "market-C"
    );
    expect(bodyRows()).toHaveLength(1);
  });
});
