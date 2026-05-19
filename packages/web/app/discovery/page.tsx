/**
 * /discovery — Candidate leader pool (Phase 2)
 *
 * Server Component: reads candidate_leaders via Drizzle, passes data to
 * a client island (DiscoveryTable) that uses shadcn data-table (TanStack).
 */

import Link from "next/link";
import { desc } from "drizzle-orm";

import { db } from "@/lib/db";
import { candidateLeaders } from "@/lib/db/schema";
import { t } from "@/lib/i18n";
import { DiscoveryTable } from "./discovery-table";

// Reads the database on every request — never prerender at build time
// (no DB is available during `next build` / CI).
export const dynamic = "force-dynamic";

// Drizzle-inferred row type (no `any`)
export type CandidateLeaderRow = typeof candidateLeaders.$inferSelect;

export default async function DiscoveryPage() {
  const rows: CandidateLeaderRow[] = await db
    .select()
    .from(candidateLeaders)
    .orderBy(desc(candidateLeaders.total_score));

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            {t("discovery.list.title")}
          </h1>
          <p className="max-w-[60ch] text-sm text-muted-foreground">
            {t("discovery.list.subtitle")}
          </p>
        </div>
        <Link
          href="/backtest/new"
          className="inline-flex h-9 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors duration-150 ease-out hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          {t("discovery.list.newBacktest")}
        </Link>
      </div>

      <DiscoveryTable data={rows} />
    </div>
  );
}
