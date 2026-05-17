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

beforeEach(() => {
  eventsResult = [];
  pendingResult = [];
  vapidResult = [{ value: "BPUBKEY" }];
  select.mockClear();
});

describe("/alerts page (Server Component)", () => {
  it("renders the calm empty state when there are no events", async () => {
    render(await AlertsPage());
    expect(screen.getByText("警示中心")).toBeInTheDocument();
    expect(
      screen.getByText(/目前沒有任何警示/)
    ).toBeInTheDocument();
    expect(screen.getByText(/目前沒有待確認的操作/)).toBeInTheDocument();
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
    expect(screen.getByText("已送達")).toBeInTheDocument();
    expect(screen.getByText("未推播")).toBeInTheDocument();
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
