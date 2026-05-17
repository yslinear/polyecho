import { describe, it, expect } from "vitest";
import { cn, navSection, isNavItemActive } from "@/lib/utils";

describe("cn (className merge helper)", () => {
  it("joins multiple class strings", () => {
    expect(cn("a", "b", "c")).toBe("a b c");
  });

  it("drops falsy values (conditional classes)", () => {
    expect(cn("a", false && "b", null, undefined, "c")).toBe("a c");
  });

  it("supports the clsx object syntax", () => {
    expect(cn("base", { active: true, disabled: false })).toBe("base active");
  });

  it("supports array inputs", () => {
    expect(cn(["a", "b"], "c")).toBe("a b c");
  });

  it("dedupes conflicting tailwind utilities (twMerge), last wins", () => {
    expect(cn("p-2", "p-4")).toBe("p-4");
    expect(cn("text-red-500", "text-green-500")).toBe("text-green-500");
  });

  it("keeps non-conflicting tailwind utilities", () => {
    expect(cn("px-2 py-1", "text-sm")).toBe("px-2 py-1 text-sm");
  });

  it("returns an empty string for no/empty input", () => {
    expect(cn()).toBe("");
    expect(cn(false, null, undefined)).toBe("");
  });
});

describe("navSection (href -> top-level section)", () => {
  it("maps root to itself", () => {
    expect(navSection("/")).toBe("/");
  });

  it("keeps a single-segment href as-is", () => {
    expect(navSection("/discovery")).toBe("/discovery");
    expect(navSection("/trades")).toBe("/trades");
  });

  it("reduces a sub-route href to its first segment", () => {
    expect(navSection("/backtest/new")).toBe("/backtest");
    expect(navSection("/backtest/abc-123")).toBe("/backtest");
  });

  it("ignores query/hash and a trailing slash", () => {
    expect(navSection("/backtest/")).toBe("/backtest");
    expect(navSection("/discovery?q=1")).toBe("/discovery");
    expect(navSection("/trades#top")).toBe("/trades");
  });
});

describe("isNavItemActive (sidebar section match)", () => {
  it("root href matches ONLY the exact root path", () => {
    expect(isNavItemActive("/", "/")).toBe(true);
    expect(isNavItemActive("/discovery", "/")).toBe(false);
    expect(isNavItemActive("/backtest/new", "/")).toBe(false);
  });

  it("highlights Backtest for the whole /backtest section", () => {
    expect(isNavItemActive("/backtest/new", "/backtest/new")).toBe(true);
    expect(isNavItemActive("/backtest/abc-123-uuid", "/backtest/new")).toBe(
      true
    );
    expect(isNavItemActive("/backtest", "/backtest/new")).toBe(true);
  });

  it("keeps discovery exclusivity (list + detail, not siblings)", () => {
    expect(isNavItemActive("/discovery", "/discovery")).toBe(true);
    expect(isNavItemActive("/discovery/0xabc", "/discovery")).toBe(true);
    expect(isNavItemActive("/leaders", "/discovery")).toBe(false);
  });

  it("matches only on a path-segment boundary (no prefix bleed)", () => {
    expect(isNavItemActive("/trades-extra", "/trades")).toBe(false);
    expect(isNavItemActive("/backtesting", "/backtest/new")).toBe(false);
    expect(isNavItemActive("/trades", "/trades")).toBe(true);
    expect(isNavItemActive("/trades/42", "/trades")).toBe(true);
  });

  it("normalizes trailing slash / query on the active path", () => {
    expect(isNavItemActive("/backtest/", "/backtest/new")).toBe(true);
    expect(isNavItemActive("/discovery?q=1", "/discovery")).toBe(true);
    expect(isNavItemActive("/?ref=x", "/")).toBe(true);
  });
});
