import { describe, it, expect } from "vitest";
import { cn } from "@/lib/utils";

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
