import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { t } from "@/lib/i18n";

/**
 * /positions data-shaping: open my_trades rows render with FIFO
 * matched/remaining quantities, side badge, <Pnl/> money and a stable
 * opened-at. The Drizzle client is mocked with a chainable builder that
 * resolves the terminal await to a controlled row array (no DB/network).
 *
 * Prose locators are derived from the i18n dictionary (the SAME keys the
 * page uses); numeric/date cells are language-neutral data.
 */

let rows: unknown[] = [];
function makeBuilder() {
  const builder: Record<string, unknown> = {};
  const chain = () => builder;
  for (const m of ["select", "from", "where", "orderBy", "leftJoin", "limit"]) {
    builder[m] = vi.fn(chain);
  }
  builder.then = (resolve: (v: unknown[]) => unknown) => resolve(rows);
  return builder;
}

vi.mock("@/lib/db", () => ({ db: makeBuilder() }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import PositionsPage from "@/app/positions/page";

beforeEach(() => {
  rows = [];
});

describe("/positions data-shaping", () => {
  it("renders an open position with matched/remaining FIFO qty + PnL", async () => {
    rows = [
      {
        id: 1,
        account_id: "acc-1",
        leader_address: "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        copy_of_trade_id: "t-1",
        market_id: "mkt-abc",
        side: "BUY",
        size: "100",
        price: "0.42",
        fill_status: "partial",
        pnl_usd: "12.5",
        matched_qty: "60",
        remaining_qty: "40",
        opened_at: new Date("2026-05-15T10:00:00Z"),
        closed_at: null,
      },
    ];
    render(await PositionsPage());

    expect(
      screen.getByText(t("tables.positions.subtitleCount", { count: 1 }))
    ).toBeInTheDocument();
    const row = screen.getByText("mkt-abc").closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    // Market, Leader, Side, Size, Entry, Matched, Remaining, Fill, PnL, Opened
    expect(cells[3]).toHaveTextContent("100");
    expect(cells[4]).toHaveTextContent("0.4200");
    expect(cells[5]).toHaveTextContent("60");
    expect(cells[6]).toHaveTextContent("40");
    expect(within(row).getByText("BUY")).toBeInTheDocument();
    expect(within(row).getByText("partial")).toBeInTheDocument();
    // <Pnl/> renders signed money with the directional arrow.
    expect(within(row).getByText(/\+\$12\.50/)).toBeInTheDocument();
    expect(within(row).getByText(/2026-05-15 10:00/)).toBeInTheDocument();
  });

  it("shows the calm empty state when no positions are open", async () => {
    rows = [];
    render(await PositionsPage());
    expect(
      screen.getByText(t("tables.positions.subtitleEmpty"))
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("tables.positions.emptyTitle"))
    ).toBeInTheDocument();
  });
});
