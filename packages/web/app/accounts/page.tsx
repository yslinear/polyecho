/**
 * /accounts — Account management (Phase 3, read-only)
 *
 * Server Component: lists rows from the `accounts` table. SECURITY: the
 * encrypted_private_key column is NEVER selected or displayed here — the
 * web tier is read-only and key import is CLI-only.
 */

import { asc } from "drizzle-orm";

import { db } from "@/lib/db";
import { accounts } from "@/lib/db/schema";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { signatureTypeLabel } from "@/lib/format";
import { CopyAddress } from "./copy-address";

export const dynamic = "force-dynamic";

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  ready: "default",
  pending_key_import: "secondary",
  disabled: "destructive",
};

// Explicit column projection — encrypted_private_key is intentionally omitted.
export type AccountRow = {
  id: string;
  name: string;
  eoa_address: string;
  funder_address: string | null;
  signature_type: number | null;
  status: string;
  created_at: Date;
  updated_at: Date;
};

export default async function AccountsPage() {
  const rows: AccountRow[] = await db
    .select({
      id: accounts.id,
      name: accounts.name,
      eoa_address: accounts.eoa_address,
      funder_address: accounts.funder_address,
      signature_type: accounts.signature_type,
      status: accounts.status,
      created_at: accounts.created_at,
      updated_at: accounts.updated_at,
    })
    .from(accounts)
    .orderBy(asc(accounts.name));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Accounts</h1>
        <p className="text-muted-foreground mt-1">
          Your Polymarket accounts — {rows.length} registered
        </p>
      </div>

      <Card className="border-amber-300 bg-amber-50 dark:border-amber-900/60 dark:bg-amber-950/30">
        <CardContent className="py-4">
          <p className="text-sm text-amber-800 dark:text-amber-300">
            <strong>Private key import is CLI-only.</strong> Run{" "}
            <code className="font-mono text-xs bg-amber-100 dark:bg-amber-900/50 px-1 py-0.5 rounded">
              polyecho account import &lt;eoa&gt;
            </code>{" "}
            from the trader-core host. This web dashboard is read-only and
            never accepts or displays private keys.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Registered Accounts</CardTitle>
        </CardHeader>
        <CardContent>
          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No accounts registered yet. Add one from the CLI with{" "}
              <code className="font-mono text-xs bg-muted px-1 py-0.5 rounded">
                polyecho account import &lt;eoa&gt;
              </code>
              .
            </p>
          ) : (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>EOA Address</TableHead>
                    <TableHead>Signature Type</TableHead>
                    <TableHead>Status</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((a) => (
                    <TableRow key={a.id}>
                      <TableCell className="font-medium">{a.name}</TableCell>
                      <TableCell>
                        <CopyAddress address={a.eoa_address} />
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="font-mono text-xs">
                          {signatureTypeLabel(a.signature_type)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={STATUS_VARIANT[a.status] ?? "secondary"}
                          className="capitalize text-xs"
                        >
                          {a.status.replace(/_/g, " ")}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
