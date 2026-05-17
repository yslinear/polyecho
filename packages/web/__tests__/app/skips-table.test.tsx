import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

/**
 * SkipsTable (read-only) + skip-reason taxonomy badges.
 *
 * Asserts the table renders a row per copy_skip, maps reasons to the
 * frozen SkipReason taxonomy labels/variants, and shows a calm empty state
 * when there are no skips.
 */

import { SkipsTable, type SkipRow } from "@/app/leaders/skips-table";
import {
  skipReasonLabel,
  skipReasonVariant,
} from "@/lib/skip-reasons";

const rows: SkipRow[] = [
  {
    id: 1,
    leader_address: "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    condition_id: "0xcond1",
    skip_reason: "daily_loss_cap",
    detail: "daily loss limit reached",
    created_at: new Date("2026-05-10T08:00:00Z"),
  },
  {
    id: 2,
    leader_address: null,
    condition_id: null,
    skip_reason: "dup",
    detail: null,
    created_at: new Date("2026-05-09T08:00:00Z"),
  },
];

describe("skip-reasons taxonomy", () => {
  it("maps known reasons to human labels", () => {
    expect(skipReasonLabel("daily_loss_cap")).toBe("Daily loss cap");
    expect(skipReasonLabel("dup")).toBe("Duplicate");
    expect(skipReasonLabel("data_unavailable")).toBe("Data unavailable");
    expect(skipReasonLabel("live_locked")).toBe("Live locked");
  });

  it("falls back to the raw value for an unknown reason", () => {
    expect(skipReasonLabel("totally_new_reason")).toBe(
      "totally_new_reason"
    );
    expect(skipReasonVariant("totally_new_reason")).toBe("secondary");
  });

  it("escalates risky reasons to a destructive variant", () => {
    expect(skipReasonVariant("daily_loss_cap")).toBe("destructive");
    expect(skipReasonVariant("portfolio_cap")).toBe("destructive");
    expect(skipReasonVariant("dup")).toBe("outline");
  });
});

describe("SkipsTable", () => {
  it("renders one row per skip with taxonomy reason badges", () => {
    render(<SkipsTable rows={rows} limit={50} />);
    expect(screen.getByText("Daily loss cap")).toBeInTheDocument();
    expect(screen.getByText("Duplicate")).toBeInTheDocument();
    expect(
      screen.getByText("daily loss limit reached")
    ).toBeInTheDocument();
    // shortened leader wallet for the row that has one
    expect(screen.getByText("0xaaaa…aaaa")).toBeInTheDocument();
  });

  it("uses 'Not set' for null leader/condition/detail (no em dash)", () => {
    render(<SkipsTable rows={rows} limit={50} />);
    expect(screen.getAllByText("Not set").length).toBeGreaterThanOrEqual(2);
  });

  it("shows a calm empty state when there are no skips", () => {
    render(<SkipsTable rows={[]} limit={50} />);
    expect(
      screen.getByText(/No skips logged yet\./i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/reason from the taxonomy/i)
    ).toBeInTheDocument();
  });
});
