/**
 * /backtest/new — Create a new backtest job (Phase 2)
 *
 * Server Component wrapper that reads optional ?leader= search param,
 * then renders the client-side form island.
 */

interface NewBacktestPageProps {
  searchParams: Promise<Record<string, string | undefined>>;
}

import { BacktestForm } from "./backtest-form";

export default async function NewBacktestPage({
  searchParams,
}: NewBacktestPageProps) {
  const sp = await searchParams;
  const defaultLeader = sp.leader ?? "";

  return (
    <div className="max-w-2xl space-y-8">
      <div className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">New Backtest</h1>
        <p className="max-w-[60ch] text-sm text-muted-foreground">
          Configure a backtest job. When you queue it, a worker picks it up
          and the result appears on its own page.
        </p>
      </div>

      <BacktestForm defaultLeader={defaultLeader} />
    </div>
  );
}
