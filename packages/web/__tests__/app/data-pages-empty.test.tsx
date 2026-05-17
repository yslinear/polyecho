import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

/**
 * Empty-state coverage for the read-only data pages.
 *
 * Each page is an async Server Component ending in an awaited Drizzle
 * query. We mock `db` with a chainable builder whose terminal methods
 * resolve to a row array we control. No DB, no network. We assert the
 * calm one-sentence empty state names the real next action.
 */

let rows: unknown[] = [];

// A chainable query builder: every method returns the same thenable
// builder, so any of .orderBy/.where/.leftJoin can be the terminal await.
function makeBuilder() {
  const builder: Record<string, unknown> = {};
  const chain = () => builder;
  for (const m of ["select", "from", "where", "orderBy", "leftJoin", "limit"]) {
    builder[m] = vi.fn(chain);
  }
  // Make the builder awaitable -> resolves to the current `rows`.
  builder.then = (resolve: (v: unknown[]) => unknown) => resolve(rows);
  return builder;
}

vi.mock("@/lib/db", () => ({ db: makeBuilder() }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

import PositionsPage from "@/app/positions/page";
import AccountsPage from "@/app/accounts/page";
import LeadersPage from "@/app/leaders/page";

describe("data-page empty states", () => {
  beforeEach(() => {
    rows = [];
  });

  it("/positions: calm sentence about copy trades", async () => {
    render(await PositionsPage());
    expect(
      screen.getByText(/No open positions\./i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/trader-core opens a copy/i)
    ).toBeInTheDocument();
  });

  it("/accounts: keeps CLI-only note + empty state next action", async () => {
    render(await AccountsPage());
    // Security note always present, even when empty.
    expect(
      screen.getByText(/Private-key import is CLI-only\./i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/No accounts registered yet\./i)
    ).toBeInTheDocument();
    // The real next action (the CLI command) is shown.
    expect(
      screen.getAllByText(/polyecho account import/i).length
    ).toBeGreaterThan(0);
    // Never render key material.
    expect(
      screen.queryByText(/encrypted_private_key/i)
    ).not.toBeInTheDocument();
  });

  it("/leaders: empty state points at Discovery + CLI follow", async () => {
    render(await LeadersPage());
    expect(
      screen.getByText(/Not following anyone yet\./i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/polyecho follow/i)
    ).toBeInTheDocument();
  });
});
