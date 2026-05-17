/**
 * /accounts — Account management
 * Phase 0 skeleton: wallet type detection UI comes in Phase 3.
 * Private key import is CLI-only (polyecho account import <eoa>).
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function AccountsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Accounts</h1>
        <p className="text-muted-foreground mt-1">
          Your Polymarket accounts — status and wallet type
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Registered Accounts</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            accounts table with eoa_address, signature_type, and status will
            appear here. Wallet type detection (EOA / POLY_PROXY /
            POLY_GNOSIS_SAFE / POLY_1271) launches in Phase 3.
          </p>
          <p className="text-sm text-amber-600 mt-2">
            Private key import is CLI-only:{" "}
            <code className="font-mono text-xs bg-muted px-1 py-0.5 rounded">
              polyecho account import &lt;eoa&gt;
            </code>
          </p>
          <Badge variant="outline" className="mt-3">
            Phase 3
          </Badge>
        </CardContent>
      </Card>
    </div>
  );
}
