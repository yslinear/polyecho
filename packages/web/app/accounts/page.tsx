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
import { t } from "@/lib/i18n";
import { CopyAddress } from "./copy-address";
import { ImportAccountForm } from "./import-account-form";

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
        <h1 className="text-2xl font-bold tracking-tight">
          {t("tables.accounts.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? t("tables.accounts.subtitleEmpty")
            : t("tables.accounts.subtitleCount", { count: rows.length })}
        </p>
      </header>

      <div className="flex items-start gap-3 rounded-lg border border-border bg-muted px-4 py-3">
        <Badge variant="destructive" className="mt-0.5 shrink-0 text-xs">
          {t("tables.accounts.cliOnlyBadge")}
        </Badge>
        <p className="text-sm text-foreground">
          <strong className="font-semibold">
            {t("tables.accounts.cliOnlyTitle")}
          </strong>{" "}
          {t("tables.accounts.cliOnlyBody", { cmd: "[[CMD]]" })
            .split("[[CMD]]")
            .flatMap((part, i) =>
              i === 0
                ? [part]
                : [
                    <code
                      key="cmd"
                      className="rounded bg-background px-1 py-0.5 font-mono text-xs"
                    >
                      {/* Literal CLI command — proper noun per common.ts
                          PROPER-NOUN POLICY, intentionally not translated.
                          String expression (not JSXText) so the i18n
                          regression guard correctly treats it as exempt. */}
                      {"polyecho account import <eoa>"}
                    </code>,
                    part,
                  ],
            )}
        </p>
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold tracking-tight">
          {t("tables.accounts.import.heading")}
        </h2>
        <ImportAccountForm />
      </section>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            {t("tables.accounts.emptyPrefix", { cmd: "[[CMD]]" })
              .split("[[CMD]]")
              .flatMap((part, i) =>
                i === 0
                  ? [part]
                  : [
                      <code
                        key="cmd"
                        className="rounded bg-muted px-1 py-0.5 font-mono text-xs"
                      >
                        {/* Literal CLI command — proper noun per common.ts
                            PROPER-NOUN POLICY, intentionally not translated. */}
                        {"polyecho account import <eoa>"}
                      </code>,
                      part,
                    ],
              )}
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table className="min-w-[34rem]">
            <TableHeader className="sticky top-0 z-10 bg-muted">
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("tables.accounts.col.name")}</TableHead>
                <TableHead>{t("tables.accounts.col.eoaAddress")}</TableHead>
                <TableHead>
                  {t("tables.accounts.col.signatureType")}
                </TableHead>
                <TableHead>{t("tables.accounts.col.status")}</TableHead>
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
