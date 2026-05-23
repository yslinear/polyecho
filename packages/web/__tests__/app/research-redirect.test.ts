import { describe, it, expect, vi } from "vitest";

/**
 * /research permanent redirect (Phase-1 merge).
 *
 * The paper-trading ladder UI is merged into /discovery as the chip-filtered
 * funnel. /research must permanent-redirect to /discovery?state=active (the
 * driver of the closest analogue — the 驗證中 chip).
 *
 * We mock next/navigation so the `permanentRedirect` call is observable
 * without a Next.js runtime + so it never actually throws.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const permanentRedirect = vi.fn((url: string) => {
  // Match Next's real behaviour: the symbol is thrown to abort render.
  throw new Error(`NEXT_PERMANENT_REDIRECT:${url}`);
});
// Unused on the happy path (permanentRedirect throws first) — the fallback
// `redirect(...)` call is dead code in the page module, but the mock has to
// exist so the module can import it.
const redirect = vi.fn(() => undefined);
vi.mock("next/navigation", () => ({
  permanentRedirect: (url: string) => permanentRedirect(url),
  redirect: (url: string) => redirect(url),
}));

describe("/research permanent redirect", () => {
  it("calls permanentRedirect('/discovery?state=active')", async () => {
    const mod = await import("@/app/research/page");
    expect(() => mod.default()).toThrow(/NEXT_PERMANENT_REDIRECT/);
    expect(permanentRedirect).toHaveBeenCalledWith("/discovery?state=active");
  });
});
