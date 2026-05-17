/**
 * /discovery/[wallet] — Leader detail page
 * Phase 0 skeleton: full stats, frequency analysis, and backtest history come in Phase 2.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface LeaderDetailPageProps {
  params: Promise<{ wallet: string }>;
}

export default async function LeaderDetailPage({
  params,
}: LeaderDetailPageProps) {
  const { wallet } = await params;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold break-all">Leader: {wallet}</h1>
        <p className="text-muted-foreground mt-1">
          Wallet detail, frequency stats, and backtest history
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Profile & Scores</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              profit_score, replicability_score, strategy_type, risk_flags will
              appear here.
            </p>
            <Badge variant="outline" className="mt-3">
              Phase 1
            </Badge>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Backtest History</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Linked backtest_jobs and PnL curve chart (Recharts) will appear
              here.
            </p>
            <Badge variant="outline" className="mt-3">
              Phase 2
            </Badge>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
