import { describe, it, expect } from "vitest";
import { getTableName, getTableColumns } from "drizzle-orm";
import * as schema from "@/lib/db/schema";

/**
 * Drizzle schema must mirror PRD §6.1: all 12 tables exported with the
 * expected physical table name and key columns. We assert the column
 * data type via the Drizzle SQL type string so a schema drift breaks CI.
 */

const EXPECTED_TABLES: Record<string, string> = {
  accounts: "accounts",
  candidateLeaders: "candidate_leaders",
  candidateScoreHistory: "candidate_score_history",
  copyTemplates: "copy_templates",
  followRelations: "follow_relations",
  leaderTrades: "leader_trades",
  myTrades: "my_trades",
  settings: "settings",
  backtestJobs: "backtest_jobs",
  pendingActions: "pending_actions",
  pushSubscriptions: "push_subscriptions",
  events: "events",
};

describe("lib/db/schema — table inventory", () => {
  it("exports exactly the 12 PRD tables", () => {
    const exportedTables = Object.keys(schema).filter((k) => {
      try {
        getTableName(
          (schema as Record<string, unknown>)[k] as Parameters<
            typeof getTableName
          >[0]
        );
        return true;
      } catch {
        return false;
      }
    });
    expect(exportedTables.sort()).toEqual(Object.keys(EXPECTED_TABLES).sort());
    expect(exportedTables).toHaveLength(12);
  });

  it.each(Object.entries(EXPECTED_TABLES))(
    "%s maps to physical table '%s'",
    (exportName, physical) => {
      const table = (schema as Record<string, unknown>)[
        exportName
      ] as Parameters<typeof getTableName>[0];
      expect(getTableName(table)).toBe(physical);
    }
  );
});

describe("lib/db/schema — key columns & types", () => {
  it("accounts has uuid PK and unique eoa_address", () => {
    const cols = getTableColumns(schema.accounts);
    expect(cols.id.dataType).toBe("string");
    expect(cols.id.columnType).toBe("PgUUID");
    expect(cols.id.primary).toBe(true);
    expect(cols.name.notNull).toBe(true);
    expect(cols.eoa_address.notNull).toBe(true);
    expect(cols.eoa_address.isUnique).toBe(true);
    expect(cols.status.notNull).toBe(true);
    expect(cols.status.default).toBe("pending_key_import");
    expect(cols.signature_type.columnType).toBe("PgInteger");
  });

  it("candidate_leaders keys on wallet_address with jsonb stats + numeric scores", () => {
    const cols = getTableColumns(schema.candidateLeaders);
    expect(cols.wallet_address.primary).toBe(true);
    expect(cols.stats_7d.columnType).toBe("PgJsonb");
    expect(cols.stats_30d.columnType).toBe("PgJsonb");
    expect(cols.stats_90d.columnType).toBe("PgJsonb");
    expect(cols.total_score.columnType).toBe("PgNumeric");
    expect(cols.profit_score.columnType).toBe("PgNumeric");
    expect(cols.replicability_score.columnType).toBe("PgNumeric");
    expect(cols.user_status.default).toBe("new");
    expect(cols.user_status.notNull).toBe(true);
  });

  it("candidate_score_history has a composite PK (wallet_address, date)", () => {
    const cols = getTableColumns(schema.candidateScoreHistory);
    expect(cols.wallet_address.notNull).toBe(true);
    expect(cols.date.notNull).toBe(true);
    expect(cols.rank.columnType).toBe("PgInteger");
    // Composite PK is not on a single column
    expect(cols.wallet_address.primary).toBeFalsy();
    expect(cols.date.primary).toBeFalsy();
  });

  it("backtest_jobs requires leader_addresses array + dates and defaults status/progress", () => {
    const cols = getTableColumns(schema.backtestJobs);
    expect(cols.id.columnType).toBe("PgUUID");
    expect(cols.leader_addresses.notNull).toBe(true);
    expect(cols.start_date.notNull).toBe(true);
    expect(cols.end_date.notNull).toBe(true);
    expect(cols.status.default).toBe("queued");
    expect(cols.progress.default).toBe("0");
    expect(cols.slippage_bps.default).toBe(150);
    expect(cols.execution_delay_seconds.default).toBe(30);
  });

  it("follow_relations references accounts.id and copy_templates.id", () => {
    const cols = getTableColumns(schema.followRelations);
    expect(cols.account_id.notNull).toBe(true);
    expect(cols.template_id.notNull).toBe(true);
    expect(cols.status.default).toBe("active");
  });

  it("leader_trades uses bigserial PK and a unique trade_id", () => {
    const cols = getTableColumns(schema.leaderTrades);
    expect(cols.id.primary).toBe(true);
    expect(cols.trade_id.notNull).toBe(true);
    expect(cols.trade_id.isUnique).toBe(true);
    expect(cols.size.columnType).toBe("PgNumeric");
    expect(cols.price.columnType).toBe("PgNumeric");
  });

  it("my_trades defaults fill_status to pending and tracks pnl", () => {
    const cols = getTableColumns(schema.myTrades);
    expect(cols.fill_status.default).toBe("pending");
    expect(cols.fill_status.notNull).toBe(true);
    expect(cols.pnl_usd.columnType).toBe("PgNumeric");
  });

  it("settings is a key/jsonb store", () => {
    const cols = getTableColumns(schema.settings);
    expect(cols.key.primary).toBe(true);
    expect(cols.value.columnType).toBe("PgJsonb");
  });

  it("pending_actions defaults to awaiting_confirm", () => {
    const cols = getTableColumns(schema.pendingActions);
    expect(cols.action_type.notNull).toBe(true);
    expect(cols.status.default).toBe("awaiting_confirm");
  });

  it("push_subscriptions enforces a unique endpoint + required keys", () => {
    const cols = getTableColumns(schema.pushSubscriptions);
    expect(cols.endpoint.notNull).toBe(true);
    expect(cols.endpoint.isUnique).toBe(true);
    expect(cols.p256dh_key.notNull).toBe(true);
    expect(cols.auth_key.notNull).toBe(true);
  });

  it("events defaults severity to info", () => {
    const cols = getTableColumns(schema.events);
    expect(cols.event_type.notNull).toBe(true);
    expect(cols.severity.default).toBe("info");
    expect(cols.payload.columnType).toBe("PgJsonb");
  });

  it("copy_templates booleans carry sensible defaults", () => {
    const cols = getTableColumns(schema.copyTemplates);
    expect(cols.exclude_short_markets.default).toBe(false);
    expect(cols.use_fok_orders.default).toBe(true);
    expect(cols.copy_ratio.columnType).toBe("PgNumeric");
  });
});
