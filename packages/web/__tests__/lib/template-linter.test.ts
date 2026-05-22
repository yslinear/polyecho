import { describe, it, expect } from "vitest";

/**
 * TemplateConfig risk linter (PolyHermes deep-dive B3).
 *
 * Mirrors packages/core/tests/test_template_linter.py: a clean template
 * yields no warnings; each missing/loose cap is flagged with the right
 * severity + a suggested value; high warnings sort before medium.
 */

import {
  LOOSE_DAILY_LOSS_LIMIT_USD,
  LOOSE_MAX_CONCURRENT_POSITIONS,
  LOOSE_MAX_POSITION_USD,
  hasHighSeverity,
  lintTemplateConfig,
  type LintableTemplate,
} from "@/lib/template-linter";

function clean(): LintableTemplate {
  return {
    max_position_usd: "500",
    daily_loss_limit_usd: "200",
    max_concurrent_positions: 5,
  };
}

function codes(t: LintableTemplate): Set<string> {
  return new Set(lintTemplateConfig(t).map((w) => w.code));
}

describe("lintTemplateConfig — clean config", () => {
  it("yields no warnings for a tightly-configured template", () => {
    expect(lintTemplateConfig(clean())).toEqual([]);
  });

  it("has no high-severity warning for a clean template", () => {
    expect(hasHighSeverity(lintTemplateConfig(clean()))).toBe(false);
  });

  it("does not flag caps sitting exactly at the loose ceiling", () => {
    expect(
      lintTemplateConfig({
        max_position_usd: LOOSE_MAX_POSITION_USD,
        daily_loss_limit_usd: LOOSE_DAILY_LOSS_LIMIT_USD,
        max_concurrent_positions: LOOSE_MAX_CONCURRENT_POSITIONS,
      })
    ).toEqual([]);
  });
});

describe("lintTemplateConfig — missing caps", () => {
  it("flags every cap when all are null/empty", () => {
    expect(
      codes({
        max_position_usd: null,
        daily_loss_limit_usd: "",
        max_concurrent_positions: null,
      })
    ).toEqual(
      new Set([
        "missing.max_position_usd",
        "missing.daily_loss_limit_usd",
        "missing.max_concurrent_positions",
      ])
    );
  });

  it("flags a missing position cap as high with a positive suggestion", () => {
    const w = lintTemplateConfig({ ...clean(), max_position_usd: null });
    expect(w).toHaveLength(1);
    expect(w[0].severity).toBe("high");
    expect(w[0].field).toBe("max_position_usd");
    expect(Number(w[0].suggested)).toBeGreaterThan(0);
  });

  it("flags a missing daily-loss limit as high", () => {
    const w = lintTemplateConfig({ ...clean(), daily_loss_limit_usd: null });
    expect(w[0].severity).toBe("high");
  });

  it("flags a missing concurrency cap as medium (not loss-bounding)", () => {
    const w = lintTemplateConfig({
      ...clean(),
      max_concurrent_positions: null,
    });
    expect(w[0].severity).toBe("medium");
  });
});

describe("lintTemplateConfig — loose caps", () => {
  it("flags a loose position cap as medium with the ceiling as suggestion", () => {
    const w = lintTemplateConfig({
      ...clean(),
      max_position_usd: LOOSE_MAX_POSITION_USD + 1,
    });
    expect(w).toHaveLength(1);
    expect(w[0].severity).toBe("medium");
    expect(Number(w[0].suggested)).toBe(LOOSE_MAX_POSITION_USD);
  });

  it("flags a loose daily-loss limit", () => {
    expect(
      codes({ ...clean(), daily_loss_limit_usd: LOOSE_DAILY_LOSS_LIMIT_USD + 1 })
    ).toContain("loose.daily_loss_limit_usd");
  });

  it("flags a loose concurrency cap", () => {
    expect(
      codes({
        ...clean(),
        max_concurrent_positions: LOOSE_MAX_CONCURRENT_POSITIONS + 1,
      })
    ).toContain("loose.max_concurrent_positions");
  });

  it("accepts a string-typed numeric column (Drizzle numeric)", () => {
    expect(
      codes({ ...clean(), max_position_usd: String(LOOSE_MAX_POSITION_USD + 1) })
    ).toContain("loose.max_position_usd");
  });
});

describe("lintTemplateConfig — ordering + aggregates", () => {
  it("sorts high-severity warnings before medium", () => {
    const w = lintTemplateConfig({
      max_position_usd: null, // high (missing)
      daily_loss_limit_usd: "200",
      max_concurrent_positions: LOOSE_MAX_CONCURRENT_POSITIONS + 10, // medium
    });
    expect(w.map((x) => x.severity)).toEqual(["high", "medium"]);
  });

  it("hasHighSeverity is true when a money cap is missing", () => {
    expect(
      hasHighSeverity(
        lintTemplateConfig({ ...clean(), daily_loss_limit_usd: null })
      )
    ).toBe(true);
  });

  it("hasHighSeverity is false for a loose-only template", () => {
    expect(
      hasHighSeverity(
        lintTemplateConfig({
          ...clean(),
          max_position_usd: LOOSE_MAX_POSITION_USD + 1,
        })
      )
    ).toBe(false);
  });
});
