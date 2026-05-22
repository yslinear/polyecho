/**
 * /backtest/new — Create a new backtest job (Phase 2)
 *
 * Server Component wrapper. Reads two optional search params, then
 * renders the client-side form island:
 *   - `?leader=0x…`  pre-fills only the Leader address.
 *   - `?from=<jobId>` (the list's 「重新執行」 deep-link) pre-fills the
 *     WHOLE form from an existing job via `loadBacktestJobDefaults`.
 * `from` wins over `leader` when both are present (re-run is the
 * more specific intent).
 */

interface NewBacktestPageProps {
  searchParams: Promise<Record<string, string | undefined>>;
}

import { t } from "@/lib/i18n";
import { BacktestForm } from "./backtest-form";
import { loadBacktestJobDefaults } from "../actions";

export default async function NewBacktestPage({
  searchParams,
}: NewBacktestPageProps) {
  const sp = await searchParams;

  // `?from=<jobId>` re-run pre-fill: hydrate the full form from the
  // source job. A missing/unknown id degrades to an empty form (the
  // form keeps its own defaults), never an error.
  const fromDefaults = sp.from
    ? await loadBacktestJobDefaults(sp.from)
    : null;

  const defaultLeader = fromDefaults?.leader_address ?? sp.leader ?? "";

  return (
    <div className="max-w-2xl space-y-8">
      <div className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">
          {t("backtest.new.title")}
        </h1>
        <p className="max-w-[60ch] text-sm text-muted-foreground">
          {t("backtest.new.desc")}
        </p>
      </div>

      <BacktestForm
        defaultLeader={defaultLeader}
        initialValues={fromDefaults ?? undefined}
      />
    </div>
  );
}
