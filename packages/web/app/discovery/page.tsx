/**
 * /discovery — Candidate leader pool
 * Phase 0 skeleton: scoring table and filters come in Phase 1.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function DiscoveryPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Discovery</h1>
        <p className="text-muted-foreground mt-1">
          Candidate leader pool — sorted by total_score
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Candidate Pool</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Scored candidate_leaders with filters (strategy type, score, status)
            and backtest launch button will appear here.
          </p>
          <Badge variant="outline" className="mt-3">
            Phase 1
          </Badge>
        </CardContent>
      </Card>
    </div>
  );
}
