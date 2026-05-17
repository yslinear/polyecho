import { describe, it, expect } from "vitest";

import {
  RISK_MODE_KEY,
  GLOBAL_PAUSED_KEY,
  VAPID_PUBLIC_KEY,
  coerceStringSetting,
  coerceBoolSetting,
  coerceRiskMode,
} from "@/lib/settings-keys";

/**
 * lib/settings-keys — the single source of truth for settings row keys +
 * the bare-string/{value} coercion shared by the layout reader and the
 * Server-Action writers. Drift here would silently break trader-core reads.
 */

describe("settings keys are the frozen contract strings", () => {
  it("uses the exact key names p5-core / Python read", () => {
    expect(RISK_MODE_KEY).toBe("risk_mode");
    expect(GLOBAL_PAUSED_KEY).toBe("global_paused");
    expect(VAPID_PUBLIC_KEY).toBe("vapid_public_key");
  });
});

describe("coerceStringSetting (bare string OR {value:...})", () => {
  it("returns a bare string unchanged", () => {
    expect(coerceStringSetting("http://x")).toBe("http://x");
  });
  it("unwraps the {value:...} object shape", () => {
    expect(coerceStringSetting({ value: "abc" })).toBe("abc");
  });
  it("returns empty for null/number/odd shapes", () => {
    expect(coerceStringSetting(null)).toBe("");
    expect(coerceStringSetting(42)).toBe("");
    expect(coerceStringSetting({ nope: 1 })).toBe("");
  });
});

describe("coerceBoolSetting", () => {
  it("is true only for exactly 'true' (any case, trimmed)", () => {
    expect(coerceBoolSetting("true")).toBe(true);
    expect(coerceBoolSetting(" TRUE ")).toBe(true);
    expect(coerceBoolSetting({ value: "true" })).toBe(true);
  });
  it("is false for anything else", () => {
    expect(coerceBoolSetting("false")).toBe(false);
    expect(coerceBoolSetting("")).toBe(false);
    expect(coerceBoolSetting(null)).toBe(false);
    expect(coerceBoolSetting("1")).toBe(false);
  });
});

describe("coerceRiskMode", () => {
  it("returns 'conservative' only for the exact value", () => {
    expect(coerceRiskMode("conservative")).toBe("conservative");
    expect(coerceRiskMode({ value: "conservative" })).toBe("conservative");
  });
  it("defaults to the safe 'normal' for anything else", () => {
    expect(coerceRiskMode("normal")).toBe("normal");
    expect(coerceRiskMode("")).toBe("normal");
    expect(coerceRiskMode(null)).toBe("normal");
    expect(coerceRiskMode("aggressive")).toBe("normal");
  });
});
