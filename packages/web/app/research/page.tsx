/**
 * /research — PERMANENT REDIRECT.
 *
 * The paper-trading ladder UI is merged into /discovery as the chip-filtered
 * "候選漏斗" funnel page (Phase-1 merge, operator-approved Option C). The
 * "驗證中" chip (`?state=active`) is the closest analogue of the legacy
 * /research view (candidate + paper + cooldown), so this server-side
 * `redirect` lands the operator there.
 *
 * The promote-to-live Server Action stays in `app/research/actions.ts` (the
 * operator scoped it so) and is imported by the merged /discovery client
 * island. Only this page.tsx becomes a stub — `research-table.tsx` is
 * deleted because the merged DiscoveryTable owns the rendering now.
 *
 * Why server-side redirect: `next/navigation`'s `redirect` throws a
 * `NEXT_REDIRECT` symbol during server render, which Next translates into a
 * 307 (or 308 for permanent) HTTP redirect. No client JS runs; the operator
 * never sees the stub markup. `permanent: true` flips this to 308 so search
 * engines / bookmarks update their target.
 */

import { redirect, permanentRedirect } from "next/navigation";

// Force dynamic so the redirect is evaluated on every request, never cached
// at build time (no DB read here, but matches the discovery page idiom).
export const dynamic = "force-dynamic";

export default function ResearchPage(): never {
  // `permanentRedirect` issues a 308 — the merge is permanent, not a
  // temporary diversion. `redirect()` (307) would also work but lets stale
  // bookmarks keep hitting this route forever.
  permanentRedirect("/discovery?state=active");
  // unreachable — `permanentRedirect` throws. The explicit `redirect` import
  // above is intentionally KEPT so a future operator can swap to 307 by
  // changing one line.
  redirect("/discovery?state=active");
}
