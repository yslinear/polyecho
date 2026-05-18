import { describe, it, expect } from "vitest";
import {
  toNum,
  sumPnl,
  formatUsd,
  formatSignedUsd,
  pnlColor,
  shortAddress,
  formatDateTime,
  formatDate,
  signatureTypeLabel,
  isOpenPosition,
  startOfTodayUtc,
  sumPnlSince,
} from "@/lib/format";

describe("toNum", () => {
  it("parses numeric strings and numbers", () => {
    expect(toNum("12.5")).toBe(12.5);
    expect(toNum(42)).toBe(42);
    expect(toNum("-3")).toBe(-3);
  });
  it("returns null for null/undefined/empty/non-finite", () => {
    expect(toNum(null)).toBeNull();
    expect(toNum(undefined)).toBeNull();
    expect(toNum("")).toBeNull();
    expect(toNum("abc")).toBeNull();
  });
});

describe("sumPnl", () => {
  it("sums nullable values ignoring nulls", () => {
    expect(sumPnl(["10.5", null, "4.5", undefined, 5])).toBe(20);
  });
  it("returns 0 for an empty list", () => {
    expect(sumPnl([])).toBe(0);
  });
});

describe("formatUsd / formatSignedUsd", () => {
  it("formats positive, negative and null", () => {
    expect(formatUsd("1234.5")).toBe("$1,234.50");
    expect(formatUsd("-1234.5")).toBe("-$1,234.50");
    expect(formatUsd(null)).toBe("$0.00");
  });
  it("adds an explicit + only for positive PnL", () => {
    expect(formatSignedUsd("12")).toBe("+$12.00");
    expect(formatSignedUsd("-12")).toBe("-$12.00");
    expect(formatSignedUsd(0)).toBe("$0.00");
    expect(formatSignedUsd(null)).toBe("$0.00");
  });
});

describe("pnlColor", () => {
  it("uses gain/loss tokens for +/-, muted for zero/null (DESIGN.md)", () => {
    expect(pnlColor("5")).toBe("text-gain");
    expect(pnlColor("-5")).toBe("text-loss");
    expect(pnlColor(0)).toBe("text-muted-foreground");
    expect(pnlColor(null)).toBe("text-muted-foreground");
  });
});

describe("shortAddress", () => {
  it("shortens a long 0x address", () => {
    expect(
      shortAddress("0xabcdef0123456789abcdef0123456789abcdef01")
    ).toBe("0xabcd…ef01");
  });
  it("returns short input unchanged and a textual placeholder for nullish", () => {
    expect(shortAddress("0x1234")).toBe("0x1234");
    expect(shortAddress(null)).toBe("Not set");
    expect(shortAddress(undefined)).toBe("Not set");
  });
});

describe("formatDateTime / formatDate", () => {
  it("renders a stable UTC string", () => {
    const d = new Date("2026-05-17T08:09:00.000Z");
    expect(formatDateTime(d)).toBe("2026-05-17 08:09");
    expect(formatDate(d)).toBe("2026-05-17");
  });
  it("accepts ISO strings and handles invalid/null", () => {
    expect(formatDateTime("2026-01-02T03:04:00.000Z")).toBe(
      "2026-01-02 03:04"
    );
    expect(formatDateTime(null)).toBe("Not recorded");
    expect(formatDate("not-a-date")).toBe("Not recorded");
  });
});

describe("signatureTypeLabel", () => {
  it("maps known codes and falls back to Unknown", () => {
    expect(signatureTypeLabel(0)).toBe("EOA");
    expect(signatureTypeLabel(1)).toBe("POLY_PROXY");
    expect(signatureTypeLabel(2)).toBe("POLY_GNOSIS_SAFE");
    expect(signatureTypeLabel(3)).toBe("POLY_1271");
    expect(signatureTypeLabel(null)).toBe("Unknown");
    expect(signatureTypeLabel(99)).toBe("Unknown");
  });
});

describe("isOpenPosition", () => {
  it("is open when opened_at set and closed_at null", () => {
    expect(
      isOpenPosition({ opened_at: new Date(), closed_at: null })
    ).toBe(true);
  });
  it("is not open when closed_at set or opened_at null", () => {
    expect(
      isOpenPosition({ opened_at: new Date(), closed_at: new Date() })
    ).toBe(false);
    expect(isOpenPosition({ opened_at: null, closed_at: null })).toBe(false);
  });
});

describe("startOfTodayUtc / sumPnlSince", () => {
  it("returns UTC midnight for the given day", () => {
    const start = startOfTodayUtc(new Date("2026-05-17T15:30:00.000Z"));
    expect(start.toISOString()).toBe("2026-05-17T00:00:00.000Z");
  });
  it("sums only trades opened on/after the cutoff", () => {
    const since = new Date("2026-05-17T00:00:00.000Z");
    const rows = [
      { pnl_usd: "10", opened_at: new Date("2026-05-17T01:00:00.000Z") },
      { pnl_usd: "5", opened_at: new Date("2026-05-16T23:00:00.000Z") }, // before
      { pnl_usd: "3", opened_at: null }, // skipped
      { pnl_usd: null, opened_at: new Date("2026-05-17T02:00:00.000Z") },
      { pnl_usd: "7", opened_at: "2026-05-18T00:00:00.000Z" },
    ];
    expect(sumPnlSince(rows, since)).toBe(17);
  });
});
