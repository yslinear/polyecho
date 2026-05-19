import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * /alerts 警示中心 — page render + severity filter + empty state.
 *
 * The page is a Server Component reading events + the vapid_public_key row
 * through a mocked Drizzle chain. §5.7 dangerous ops are now an INLINE
 * single-step confirm (no separate awaiting-confirm pending list is read
 * or rendered here), so the page only queries events + the vapid row.
 * next/navigation + sonner + the alerts actions are mocked so nothing
 * touches the network.
 */

let eventsResult: unknown[] = [];
let vapidResult: unknown[] = [];

// events: .orderBy().limit() (terminal); vapid settings row: .where().limit().
const limit = vi.fn(() => Promise.resolve(vapidResult));
const where = vi.fn(() => ({ limit }));
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
  confirmDangerousAction: vi.fn(),
  CONFIRM_PHRASE: "確認執行",
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
  vapidResult = [{ value: "BPUBKEY" }];
  select.mockClear();
});

describe("/alerts page (Server Component)", () => {
  it("renders the calm empty state + the inline danger control", async () => {
    render(await AlertsPage());
    expect(
      screen.getByText(t("alertsSettings.alerts.title"))
    ).toBeInTheDocument();
    expect(screen.getByText(EMPTY_ALL)).toBeInTheDocument();
    // §5.7 inline danger-op control is present (collapsed open button) —
    // there is NO separate "awaiting confirmation" pending list anymore.
    expect(
      screen.getByRole("button", {
        name: t("alertsSettings.alerts.danger.openButton"),
      })
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

  it("still surfaces a pending_dangerous_action EVENT in the events list", async () => {
    // The inline confirm path emits a pending_dangerous_action event; the
    // events list (lib/alerts label map) still humanises it even though
    // there is no longer a separate pending-row panel.
    eventsResult = [
      {
        id: 7,
        event_type: "pending_dangerous_action",
        severity: "warning",
        payload: { data: { action_type: "emergency_liquidate" } },
        pushed_at: null,
        created_at: new Date("2026-05-18T10:00:00Z"),
      },
    ];
    render(await AlertsPage());
    expect(screen.getByText("待確認的危險操作")).toBeInTheDocument();
  });
});
