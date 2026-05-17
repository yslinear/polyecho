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
import { DiscoveryTable } from "./discovery-table";

// Drizzle-inferred row type (no `any`)
export type CandidateLeaderRow = typeof candidateLeaders.$inferSelect;

export default async function DiscoveryPage() {
  const rows: CandidateLeaderRow[] = await db
    .select()
    .from(candidateLeaders)
    .orderBy(desc(candidateLeaders.total_score));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">Discovery</h1>
          <p className="text-muted-foreground mt-1">
            Candidate leader pool — {rows.length} leaders
          </p>
        </div>
        <Link
          href="/backtest/new"
          className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          New Backtest
        </Link>
      </div>

      <DiscoveryTable data={rows} />
    </div>
  );
}
