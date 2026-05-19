import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { t } from "@/lib/i18n";

/**
 * Empty-state coverage for the read-only data pages.
 *
 * Each page is an async Server Component ending in an awaited Drizzle
 * query. We mock `db` with a chainable builder whose terminal methods
 * resolve to a row array we control. No DB, no network. We assert the
 * calm one-sentence empty state names the real next action.
 *
 * Prose locators are derived from the i18n dictionary (the SAME keys the
 * pages use); the CLI command literals are real <code> next actions
 * rendered verbatim (not translated) and asserted directly.
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

// i18n prose is often split across an inline <code>/<span>/<Link>, so the
// sentence is broken across multiple DOM nodes. Match the INNERMOST element
// whose whitespace-normalised full textContent contains `frag` (the leading
// clause derived from the dict) — robust to that splitting, and unambiguous
// (ancestors are excluded because a child also matches).
function brokenText(frag: string) {
  const norm = (s: string | null | undefined) =>
    (s ?? "").replace(/\s+/g, " ").trim();
  const needle = norm(frag);
  return (_content: string, node: Element | null) => {
    if (!node) return false;
    const has = (el: Element | null | undefined) =>
      norm(el?.textContent).includes(needle);
    return has(node) && Array.from(node.children).every((c) => !has(c));
  };
}

describe("data-page empty states", () => {
  beforeEach(() => {
    rows = [];
  });

  it("/positions: calm sentence about copy trades", async () => {
    render(await PositionsPage());
    expect(
      screen.getByText(t("tables.positions.subtitleEmpty"))
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("tables.positions.emptyTitle"))
    ).toBeInTheDocument();
  });

  it("/accounts: keeps CLI-only note + empty state next action", async () => {
    render(await AccountsPage());
    // Security note always present, even when empty (standalone <strong>).
    expect(
      screen.getByText(t("tables.accounts.cliOnlyTitle"))
    ).toBeInTheDocument();
    // Empty-state prose: the leading clause before the inline <code>.
    const emptyPrefix = t("tables.accounts.emptyPrefix", {
      cmd: "[[CMD]]",
    }).split("[[CMD]]")[0];
    expect(screen.getByText(brokenText(emptyPrefix))).toBeInTheDocument();
    // The real next action (the CLI command) is shown verbatim.
    expect(
      screen.getAllByText(/polyecho account import/i).length
    ).toBeGreaterThan(0);
    // Never render key material.
    expect(
      screen.queryByText(/encrypted_private_key/i)
    ).not.toBeInTheDocument();
  });

  it("/leaders: empty state points at Discovery + the real CLI next step", async () => {
    render(await LeadersPage());
    expect(
      screen.getByText(brokenText(t("tables.leaders.emptyPrefix")))
    ).toBeInTheDocument();
    // Discovery is highlighted in its own span.
    expect(
      screen.getByText(t("tables.leaders.emptyDiscovery"))
    ).toBeInTheDocument();
    // The real CLI next step is rendered verbatim in a <code>.
    expect(
      screen.getByText(/polyecho discovery promote/i)
    ).toBeInTheDocument();
  });
});
