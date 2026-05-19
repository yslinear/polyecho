import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * /alerts 警示中心 — page render + severity filter + empty state.
 *
 * The page is a Server Component reading events + pending_actions + the
 * vapid_public_key row through a mocked Drizzle chain. next/navigation +
 * sonner + the alerts actions are mocked so nothing touches the network.
 */

let eventsResult: unknown[] = [];
let pendingResult: unknown[] = [];
let vapidResult: unknown[] = [];

// events: .orderBy().limit() (terminal); pending: .where().orderBy();
// vapid settings row: .where().limit().
const orderByPending = vi.fn(() => Promise.resolve(pendingResult));
const limit = vi.fn(() => Promise.resolve(vapidResult));
const where = vi.fn(() => ({ orderBy: orderByPending, limit }));
const from = vi.fn(() => {
  // events: select().from(events).orderBy().limit()
  return {
    orderBy: () => ({ limit: () => Promise.resolve(eventsResult) }),
    where,
  };
});
const select = vi.fn(() => ({ from }));

vi.mock("@/lib/db", () => ({
  db: { select: (...a: unknown[]) => select(...a) },
}));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/app/alerts/actions", () => ({
  getVapidPublicKey: vi.fn(async () => "BPUBKEY"),
  subscribePush: vi.fn(),
  unsubscribePush: vi.fn(),
  createPendingAction: vi.fn(),
  confirmPendingAction: vi.fn(),
}));

import AlertsPage from "@/app/alerts/page";
import { t } from "@/lib/i18n";

// Island (severity-filter) locators derive from the i18n dictionary so the
// test asserts the filter BEHAVIOUR / delivered-state, not hardcoded copy,
// and stays correct if a zh-TW string changes. (event_type / pending-action
// labels come from lib/alerts.ts and are asserted as that frozen contract.)
const EMPTY_ALL = t("alertsSettings.alerts.list.emptyAll");
const DELIVERED = t("alertsSettings.alerts.list.delivered");
const NOT_PUSHED = t("alertsSettings.alerts.list.notPushed");

beforeEach(() => {
  eventsResult = [];
  pendingResult = [];
  vapidResult = [{ value: "BPUBKEY" }];
  select.mockClear();
});

describe("/alerts page (Server Component)", () => {
  it("renders the calm empty state when there are no events", async () => {
    render(await AlertsPage());
    expect(
      screen.getByText(t("alertsSettings.alerts.title"))
    ).toBeInTheDocument();
    expect(screen.getByText(EMPTY_ALL)).toBeInTheDocument();
    expect(
      screen.getByText(t("alertsSettings.alerts.pending.empty"))
    ).toBeInTheDocument();
  });

  it("lists events newest-first with humanised type + delivered state", async () => {
    eventsResult = [
      {
        id: 2,
        event_type: "stop_loss",
        severity: "critical",
        payload: { data: { leader: "0xabc", pnl: -120 } },
        pushed_at: new Date("2026-05-18T10:00:00Z"),
        created_at: new Date("2026-05-18T10:00:00Z"),
      },
      {
        id: 1,
        event_type: "copy_fill",
        severity: "info",
        payload: { data: { market: "m1" } },
        pushed_at: null,
        created_at: new Date("2026-05-18T09:00:00Z"),
      },
    ];
    render(await AlertsPage());
    expect(screen.getByText("停損觸發")).toBeInTheDocument();
    expect(screen.getByText("跟單成交")).toBeInTheDocument();
    expect(screen.getByText(DELIVERED)).toBeInTheDocument();
    expect(screen.getByText(NOT_PUSHED)).toBeInTheDocument();
  });

  it("filters by severity client-side", async () => {
    eventsResult = [
      {
        id: 2,
        event_type: "stop_loss",
        severity: "critical",
        payload: {},
        pushed_at: null,
        created_at: new Date("2026-05-18T10:00:00Z"),
      },
      {
        id: 1,
        event_type: "copy_fill",
        severity: "info",
        payload: {},
        pushed_at: null,
        created_at: new Date("2026-05-18T09:00:00Z"),
      },
    ];
    render(await AlertsPage());
    expect(screen.getByText("停損觸發")).toBeInTheDocument();
    expect(screen.getByText("跟單成交")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "CRITICAL", pressed: false })
    );
    expect(screen.getByText("停損觸發")).toBeInTheDocument();
    expect(screen.queryByText("跟單成交")).not.toBeInTheDocument();
  });

  it("shows an awaiting pending action with its label and params", async () => {
    pendingResult = [
      {
        id: "pa-1",
        action_type: "emergency_liquidate",
        params: { target: "acct-1" },
        expires_at: new Date(Date.now() + 120_000),
        created_at: new Date("2026-05-18T10:00:00Z"),
      },
    ];
    render(await AlertsPage());
    expect(screen.getByText("緊急清倉")).toBeInTheDocument();
    expect(screen.getByText(/target: acct-1/)).toBeInTheDocument();
  });
});
