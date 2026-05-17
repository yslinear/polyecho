/**
 * /accounts — Account management (read-only).
 *
 * Server Component: lists rows from the `accounts` table. SECURITY: the
 * encrypted_private_key column is NEVER selected or displayed — the web
 * tier is read-only and private-key import is CLI-only.
 */

import { asc } from "drizzle-orm";

import { db } from "@/lib/db";
import { accounts } from "@/lib/db/schema";
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
    <div className="space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Accounts</h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? "Polymarket accounts you have registered from the CLI."
            : `${rows.length} registered account${rows.length === 1 ? "" : "s"}.`}
        </p>
      </header>

      <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-900/60 dark:bg-amber-950/30">
        <p className="text-sm text-amber-900 dark:text-amber-200">
          <strong className="font-semibold">
            Private-key import is CLI-only.
          </strong>{" "}
          Run{" "}
          <code className="rounded bg-amber-100 px-1 py-0.5 font-mono text-xs dark:bg-amber-900/50">
            polyecho account import &lt;eoa&gt;
          </code>{" "}
          on the trader-core host. This dashboard is read-only and never
          accepts or displays private keys.
        </p>
      </div>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            No accounts registered yet. Import one from the CLI with{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
              polyecho account import &lt;eoa&gt;
            </code>
            .
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted/60 backdrop-blur supports-[backdrop-filter]:bg-muted/60">
              <TableRow className="hover:bg-transparent">
                <TableHead>Name</TableHead>
                <TableHead>EOA address</TableHead>
                <TableHead>Signature type</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((a) => (
                <TableRow key={a.id} className="hover:bg-muted/50">
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
    </div>
  );
}
