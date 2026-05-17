/**
 * /leaders — Active copy-trade relationships
 * Phase 0 skeleton: pause/resume controls come in Phase 4.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function LeadersPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Leaders</h1>
        <p className="text-muted-foreground mt-1">
          Followed leaders and copy-trade status
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Followed Leaders</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Active follow_relations with pause/resume controls will appear here.
          </p>
          <Badge variant="outline" className="mt-3">
            Phase 4
          </Badge>
        </CardContent>
      </Card>
    </div>
  );
}
