import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { t } from "@/lib/i18n";

/**
 * SkipsTable (read-only) + skip-reason taxonomy badges.
 *
 * Asserts the table renders a row per copy_skip, maps reasons to the
 * frozen SkipReason taxonomy labels/variants, and shows a calm empty
 * state when there are no skips. Locators are derived from the i18n
 * dictionary (the SAME keys lib/skip-reasons + the island use), so the
 * test asserts STRUCTURE/behaviour and stays correct if a translation
 * string changes.
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
  it("maps known reasons to the localized taxonomy label", () => {
    // The label is the i18n value for that reason's dict key — derived
    // from the dictionary so a translation edit doesn't break the test,
    // while still proving the taxonomy→i18n seam resolves a real string.
    expect(skipReasonLabel("daily_loss_cap")).toBe(
      t("tables.skipReasons.dailyLossCap")
    );
    expect(skipReasonLabel("dup")).toBe(t("tables.skipReasons.dup"));
    expect(skipReasonLabel("data_unavailable")).toBe(
      t("tables.skipReasons.dataUnavailable")
    );
    expect(skipReasonLabel("live_locked")).toBe(
      t("tables.skipReasons.liveLocked")
    );
    // Each known reason resolves to a non-empty string that is NOT the
    // raw enum value (i.e. the dict key actually exists).
    for (const reason of ["daily_loss_cap", "dup", "live_locked"]) {
      const label = skipReasonLabel(reason);
      expect(label.length).toBeGreaterThan(0);
      expect(label).not.toBe(reason);
    }
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
    expect(
      screen.getByText(t("tables.skipReasons.dailyLossCap"))
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("tables.skipReasons.dup"))
    ).toBeInTheDocument();
    expect(
      screen.getByText("daily loss limit reached")
    ).toBeInTheDocument();
    // shortened leader wallet for the row that has one
    expect(screen.getByText("0xaaaa…aaaa")).toBeInTheDocument();
  });

  it("uses the not-set fallback for null leader/condition/detail (no em dash)", () => {
    render(<SkipsTable rows={rows} limit={50} />);
    // row 2 has null leader, condition AND detail -> 3 cells fall back.
    expect(
      screen.getAllByText(t("tables.common.notSet")).length
    ).toBeGreaterThanOrEqual(2);
  });

  it("shows a calm empty state when there are no skips", () => {
    render(<SkipsTable rows={[]} limit={50} />);
    expect(
      screen.getByText(t("tables.leaders.skips.subtitleEmpty"))
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("tables.leaders.skips.emptyTitle"))
    ).toBeInTheDocument();
  });
});
