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
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-3xl font-bold">New Backtest</h1>
        <p className="text-muted-foreground mt-1">
          Configure a backtest job — results appear at{" "}
          <code className="text-xs bg-muted px-1 rounded">/backtest/[id]</code>
          .
        </p>
      </div>

      <BacktestForm defaultLeader={defaultLeader} />
    </div>
  );
}
