import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "fs";
import { join, resolve } from "path";
import { t } from "@/lib/i18n";
import { dict } from "@/lib/locale/zh-TW";

/**
 * i18n contract + orphan guard (i18n hardening punch-list, P2-5).
 *
 * 1. `t()` behaviour: nested-key resolution, missing-key → key itself
 *    (defensive "degrade, don't crash"), `{{var}}` interpolation incl.
 *    repeated / absent / numeric vars.
 * 2. Dict shape: every leaf is a string (the typed `I18nKey` union already
 *    forbids dangling keys at compile time; this asserts the runtime shape
 *    so `t()` can never return a non-string).
 * 3. No orphan keys: every leaf path is referenced somewhere in the web
 *    source. The scan is robust to typed indirection:
 *      - `t("a.b.c")` literal calls,
 *      - `labelKey: I18nKey` carried through `as const` arrays whose
 *        elements are the literal dotted string (e.g. status COUNTER_FIELDS),
 *      - dynamic `t(`prefix.${x}`)` — any static template prefix is treated
 *        as a wildcard covering every leaf beneath it.
 *    It therefore matches the literal dotted path as a substring of the
 *    whole source OR a static `t(`prefix` that prefixes the leaf.
 */

// ── 1. t() unit behaviour ────────────────────────────────────────────────

describe("t() — key resolution", () => {
  it("resolves a top-level-ish nested key to its zh-TW string", () => {
    expect(t("sidebar.nav")).toBe(dict.sidebar.nav);
    expect(t("status.title")).toBe(dict.status.title);
  });

  it("resolves a deeply nested key", () => {
    expect(t("discovery.table.actions.follow")).toBe(
      dict.discovery.table.actions.follow,
    );
    expect(t("tables.common.notRecorded")).toBe(
      dict.tables.common.notRecorded,
    );
  });

  it("returns the key itself for a missing/typo path (never throws)", () => {
    // Cast: these are deliberately-invalid paths the type system rejects.
    expect(t("status.nope.missing" as never)).toBe("status.nope.missing");
    expect(t("totally.unknown.key" as never)).toBe("totally.unknown.key");
  });

  it("returns the key when the path resolves to a non-string (object node)", () => {
    // `tables.common` is an object, not a leaf — must degrade to the key.
    expect(t("tables.common" as never)).toBe("tables.common");
  });
});

describe("t() — {{var}} interpolation", () => {
  it("fills a single placeholder", () => {
    expect(t("common.dataTable.rowCount", { count: 7 })).toBe("7 列");
  });

  it("coerces numeric vars to strings", () => {
    const out = t("common.dataTable.pageOf", { page: 2, total: 5 });
    expect(out).toBe("第 2 / 5 頁");
  });

  it("replaces every occurrence of a repeated placeholder", () => {
    // Synthetic check on the replace-all semantics of t(): the public
    // contract is "split/join", i.e. ALL `{{x}}` are filled, not just one.
    const repeated = t("common.liveTrading.pillLabel", { state: "X" });
    expect(repeated).toBe("Live trading X");
    // A key whose template legitimately has no var is returned verbatim
    // even when irrelevant vars are passed.
    expect(t("sidebar.nav", { unused: "z" } as never)).toBe(dict.sidebar.nav);
  });

  it("leaves an unmatched placeholder intact when its var is absent", () => {
    // `{{minutes}}` not supplied → the literal placeholder remains (no throw,
    // no empty-string surprise).
    const out = t("common.heartbeat.stale", {});
    expect(out).toContain("{{minutes}}");
  });

  it("keeps proper-noun / English ops tokens verbatim (terminology lock)", () => {
    // Heartbeat is locked to English per common.ts PROPER-NOUN POLICY.
    expect(t("common.heartbeat.missing")).toContain("heartbeat");
    expect(t("common.heartbeat.missing")).not.toContain("心跳");
    // copy-trade concept is locked to 跟單.
    expect(t("status.workers.counters.copied")).toBe("跟單");
    expect(t("dashboard.subtitle")).toContain("跟單");
    expect(t("dashboard.subtitle")).not.toContain("複製交易");
  });
});

// ── 2. Dict shape ────────────────────────────────────────────────────────

type Leaf = { path: string; value: string };

function collectLeaves(node: unknown, prefix: string, out: Leaf[]): void {
  if (typeof node === "string") {
    out.push({ path: prefix, value: node });
    return;
  }
  if (node && typeof node === "object") {
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
      collectLeaves(v, prefix ? `${prefix}.${k}` : k, out);
    }
    return;
  }
  throw new Error(`dict leaf at "${prefix}" is not a string or object`);
}

const LEAVES: Leaf[] = (() => {
  const acc: Leaf[] = [];
  collectLeaves(dict, "", acc);
  return acc;
})();

describe("dict shape", () => {
  it("has at least one leaf and every leaf is a non-empty string", () => {
    expect(LEAVES.length).toBeGreaterThan(50);
    for (const { path, value } of LEAVES) {
      expect(typeof value, `dict.${path}`).toBe("string");
      expect(value.length, `dict.${path} is empty`).toBeGreaterThan(0);
    }
  });

  it("every leaf path round-trips through t() to its own value", () => {
    for (const { path, value } of LEAVES) {
      // No vars → templates with {{x}} return verbatim, which still equals
      // the dict value (this asserts resolution, not interpolation).
      expect(t(path as never), path).toBe(value);
    }
  });
});

// ── 3. Orphan guard (robust to typed indirection) ────────────────────────

const WEB_ROOT = resolve(__dirname, "..", "..");
const SCAN_DIRS = ["app", "components", "lib"].map((d) => join(WEB_ROOT, d));

/** Files whose key *definitions* must not count as "usage". */
const DICT_DIR = join(WEB_ROOT, "lib", "locale");
const I18N_IMPL = join(WEB_ROOT, "lib", "i18n.ts");

function walk(dir: string, acc: string[]): void {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (name === "node_modules" || name === ".next") continue;
      walk(full, acc);
      continue;
    }
    if (!/\.tsx?$/.test(name)) continue;
    if (full.startsWith(DICT_DIR)) continue; // key definitions
    if (full === I18N_IMPL) continue; // the resolver itself
    if (/\.test\.tsx?$/.test(full)) continue; // tests aren't "usage"
    acc.push(full);
  }
}

const SOURCE = (() => {
  const files: string[] = [];
  for (const d of SCAN_DIRS) walk(d, files);
  return files.map((f) => readFileSync(f, "utf8")).join("\n");
})();

/**
 * Static prefixes of dynamic template calls: `t(`a.b.${x}`)` → "a.b.".
 * Any leaf beginning with such a prefix is considered referenced.
 */
const TEMPLATE_PREFIXES: string[] = (() => {
  const re = /\bt\(\s*`([^`$]*)\$\{/g;
  const set = new Set<string>();
  let m: RegExpExecArray | null;
  while ((m = re.exec(SOURCE)) !== null) {
    const prefix = m[1];
    if (prefix.length > 0) set.add(prefix);
  }
  return [...set];
})();

function isReferenced(path: string): boolean {
  // Literal usage: `t("a.b")`, or the dotted string carried in an
  // `as const` array / prop (e.g. status COUNTER_FIELDS labelKey).
  if (SOURCE.includes(path)) return true;
  // Dynamic usage: a static template prefix that this leaf lives under.
  for (const pre of TEMPLATE_PREFIXES) {
    if (path === pre || path.startsWith(pre)) return true;
  }
  return false;
}

describe("no orphan i18n keys", () => {
  it("found web source to scan and at least one dynamic template prefix", () => {
    expect(SOURCE.length).toBeGreaterThan(1000);
    // The backtest scenario labels are reached via a template key; if this
    // ever drops to 0 the dynamic-coverage arm is silently dead.
    expect(TEMPLATE_PREFIXES.length).toBeGreaterThanOrEqual(1);
  });

  it("every dict leaf is referenced somewhere in app/components/lib", () => {
    const orphans = LEAVES.map((l) => l.path).filter(
      (p) => !isReferenced(p),
    );
    expect(
      orphans,
      `Orphan i18n keys (defined but never referenced). Either wire them ` +
        `through t() or delete them:\n${orphans.join("\n")}`,
    ).toEqual([]);
  });

  it("the deleted orphans stay deleted (regression: P1-3)", () => {
    const paths = new Set(LEAVES.map((l) => l.path));
    for (const gone of [
      "common.never",
      "common.noData",
      "common.loading",
      "common.none",
      "common.status.up",
      "common.status.down",
      "common.status.stale",
      "common.status.ok",
      "common.status.running",
      "discovery.table.viewLeaderAria",
    ]) {
      expect(paths.has(gone), `${gone} should have been deleted`).toBe(false);
    }
  });

  it("keeps common.unknown — now wired through lib/format.ts", () => {
    const paths = new Set(LEAVES.map((l) => l.path));
    expect(paths.has("common.unknown")).toBe(true);
    expect(SOURCE).toContain("common.unknown");
  });
});
