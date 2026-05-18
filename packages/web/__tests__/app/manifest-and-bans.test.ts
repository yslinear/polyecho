import { describe, it, expect } from "vitest";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

import manifest from "@/app/manifest";
import {
  eventTypeLabel,
  severityMeta,
  summarisePayload,
  timeRemaining,
} from "@/lib/alerts";

const WEB = resolve(__dirname, "../..");

/**
 * PWA manifest correctness + DESIGN.md absolute-ban guardrails for the new
 * Phase-5 Wave-2 web files. These are cheap regression nets, not a lint.
 */

describe("app/manifest.ts (Next 15 MetadataRoute.Manifest)", () => {
  const m = manifest();

  it("declares the PWA fields required for iOS Web Push", () => {
    expect(m.name).toBe("PolyEcho");
    expect(m.short_name).toBe("PolyEcho");
    expect(m.display).toBe("standalone");
    expect(m.start_url).toBe("/");
    expect(m.scope).toBe("/");
    expect(m.theme_color).toMatch(/^#[0-9a-fA-F]{6}$/);
    expect(m.background_color).toMatch(/^#[0-9a-fA-F]{6}$/);
  });

  it("references 192, 512 and a maskable icon that exist as real PNGs", () => {
    const sizes = (m.icons ?? []).map((i) => i.sizes);
    expect(sizes).toContain("192x192");
    expect(sizes).toContain("512x512");
    expect((m.icons ?? []).some((i) => i.purpose === "maskable")).toBe(true);

    for (const icon of m.icons ?? []) {
      const p = resolve(WEB, "public" + icon.src);
      expect(existsSync(p)).toBe(true);
      // PNG magic number: 89 50 4E 47
      const head = readFileSync(p).subarray(0, 4);
      expect([...head]).toEqual([0x89, 0x50, 0x4e, 0x47]);
    }
  });

  it("has removed the conflicting public/manifest.json stub", () => {
    expect(existsSync(resolve(WEB, "public/manifest.json"))).toBe(false);
  });
});

describe("layout no longer hard-codes a <link rel=manifest>", () => {
  it("does not set metadata.manifest (Next injects from app/manifest.ts)", () => {
    const layout = readFileSync(resolve(WEB, "app/layout.tsx"), "utf8");
    expect(layout).not.toMatch(/manifest:\s*["']\/manifest\.json["']/);
  });
});

describe("DESIGN.md absolute bans — new Phase-5 Wave-2 files", () => {
  const files = [
    "app/alerts/page.tsx",
    "app/alerts/severity-filter.tsx",
    "app/alerts/dangerous-action.tsx",
    "app/settings/risk-mode-form.tsx",
    "components/push-subscribe.tsx",
    "components/global-pause-toggle.tsx",
    "components/trader-heartbeat-status.tsx",
  ].map((f) => ({
    name: f,
    src: readFileSync(resolve(WEB, f), "utf8"),
  }));

  /**
   * Strip JS/JSX comments so we only inspect actual UI strings, then allow
   * the standalone "—" empty-cell placeholder that the existing codebase
   * already uses (lib/format.ts, leaders-table.tsx). Any OTHER em dash —
   * i.e. one used as sentence punctuation inside prose copy — is the ban.
   */
  function uiCopyOnly(src: string): string {
    return src
      .replace(/\/\*[\s\S]*?\*\//g, "") // block comments
      .replace(/\/\/.*$/gm, "") // line comments
      .replace(/"—"/g, "") // allowed neutral placeholder token
      .replace(/>—</g, "><"); // allowed placeholder as JSX child
  }

  it("contains NO em dash in rendered UI copy (comments + placeholder allowed)", () => {
    for (const { name, src } of files) {
      expect(uiCopyOnly(src), `${name} has an em dash in UI prose`).not.toContain(
        "—"
      );
    }
  });

  it("uses no gradient-text / decorative glassmorphism utilities", () => {
    for (const { src } of files) {
      expect(src).not.toMatch(/bg-clip-text/);
      expect(src).not.toMatch(/text-transparent/);
      expect(src).not.toMatch(/\bbackdrop-blur-(xl|2xl|3xl)\b/);
    }
  });

  it("uses no side-stripe accent border utilities", () => {
    for (const { src } of files) {
      expect(src).not.toMatch(/border-l-[0-9]/);
      expect(src).not.toMatch(/border-l-(primary|gain|loss|accent)/);
    }
  });
});

describe("lib/alerts display helpers", () => {
  it("humanises known event types and passes through unknowns", () => {
    expect(eventTypeLabel("stop_loss")).toBe("停損觸發");
    expect(eventTypeLabel("candidate_high_score")).toBe("新發現高分候選");
    expect(eventTypeLabel("totally_new")).toBe("totally_new");
  });

  it("maps severity to a calm badge variant + label", () => {
    expect(severityMeta("critical")).toEqual({
      label: "CRITICAL",
      variant: "destructive",
    });
    expect(severityMeta("warning").label).toBe("WARNING");
    expect(severityMeta("info").label).toBe("INFO");
    expect(severityMeta("weird").label).toBe("INFO");
  });

  it("summarises payload.data and counts down to expiry", () => {
    expect(summarisePayload({ data: { a: 1, b: "x" } })).toBe("a: 1 · b: x");
    expect(summarisePayload({ data: "plain" })).toBe("plain");
    expect(summarisePayload(null)).toBe("");
    const now = new Date("2026-05-18T00:00:00Z");
    expect(
      timeRemaining(new Date("2026-05-18T00:02:05Z"), now)
    ).toBe("2 分 5 秒");
    expect(timeRemaining(new Date("2026-05-17T23:00:00Z"), now)).toBe(
      "已逾時"
    );
  });
});

describe("public/icons directory holds real committed PNGs", () => {
  it("contains icon-192.png and icon-512.png", () => {
    const dir = readdirSync(resolve(WEB, "public/icons"));
    expect(dir).toContain("icon-192.png");
    expect(dir).toContain("icon-512.png");
  });
});
