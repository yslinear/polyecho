/**
 * Drizzle ORM schema — mirrors PRD §6.1 exactly.
 * All tables live in the default "public" schema of the "polyecho" Postgres DB.
 */

import { sql } from "drizzle-orm";
import {
  pgTable,
  uuid,
  text,
  date,
  integer,
  bigint,
  boolean,
  jsonb,
  numeric,
  bigserial,
  customType,
  timestamp,
  primaryKey,
  unique,
  index,
  check,
} from "drizzle-orm/pg-core";

/**
 * Postgres `BYTEA` column (drizzle-orm 0.45.x has no first-class `bytea`
 * helper). Stores raw binary — used only by `accounts.encrypted_private_key`
 * (AES-256-GCM ciphertext). Mirrors the SQLAlchemy `LargeBinary` ORM column
 * and the `BYTEA` type in `infra/migrations/0000_init.sql` (3-way aligned).
 */
const bytea = customType<{ data: Buffer; driverData: Buffer }>({
  dataType() {
    return "bytea";
  },
});

// ---------------------------------------------------------------------------
// accounts — your own Polymarket accounts (PRD §5.1)
// ---------------------------------------------------------------------------
export const accounts = pgTable("accounts", {
  id: uuid("id").defaultRandom().primaryKey(),
  name: text("name").notNull(),
  eoa_address: text("eoa_address").notNull().unique(),
  funder_address: text("funder_address"),
  /** 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271 */
  signature_type: integer("signature_type"),
  /** eoa | magic | safe | poly_1271 — practical wallet identity (brief §6/§9) */
  wallet_mode: text("wallet_mode"),
  /** AES-256-GCM ciphertext (raw bytes); held only by trader-core process */
  encrypted_private_key: bytea("encrypted_private_key"),
  /** pending_key_import | ready | disabled */
  status: text("status").notNull().default("pending_key_import"),
  created_at: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updated_at: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// ---------------------------------------------------------------------------
// candidate_leaders — discovery pool (PRD §5.2)
// ---------------------------------------------------------------------------
export const candidateLeaders = pgTable("candidate_leaders", {
  wallet_address: text("wallet_address").primaryKey(),
  display_name: text("display_name"),
  sources: text("sources").array(),
  first_discovered_at: timestamp("first_discovered_at", {
    withTimezone: true,
  }),
  last_active_at: timestamp("last_active_at", { withTimezone: true }),
  stats_7d: jsonb("stats_7d"),
  stats_30d: jsonb("stats_30d"),
  stats_90d: jsonb("stats_90d"),
  frequency_stats: jsonb("frequency_stats"),
  strategy_type: text("strategy_type"),
  backtest_default: jsonb("backtest_default"),
  profit_score: numeric("profit_score", { precision: 6, scale: 2 }),
  replicability_score: numeric("replicability_score", {
    precision: 6,
    scale: 2,
  }),
  total_score: numeric("total_score", { precision: 6, scale: 2 }),
  risk_flags: text("risk_flags").array(),
  specialization: text("specialization").array(),
  /** new | watching | following | rejected */
  user_status: text("user_status").notNull().default("new"),
  user_notes: text("user_notes"),
  updated_at: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// ---------------------------------------------------------------------------
// candidate_score_history — daily snapshot (PRD §6.1)
// ---------------------------------------------------------------------------
export const candidateScoreHistory = pgTable(
  "candidate_score_history",
  {
    wallet_address: text("wallet_address").notNull(),
    date: date("date").notNull(), // Postgres DATE — matches SQLAlchemy Date model
    total_score: numeric("total_score", { precision: 6, scale: 2 }),
    rank: integer("rank"),
    stats: jsonb("stats"),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.wallet_address, t.date] }),
  })
);

// ---------------------------------------------------------------------------
// copy_templates — risk & sizing presets (PRD §5.6)
// ---------------------------------------------------------------------------
export const copyTemplates = pgTable("copy_templates", {
  id: uuid("id").defaultRandom().primaryKey(),
  name: text("name").notNull(),
  copy_ratio: numeric("copy_ratio", { precision: 8, scale: 5 }),
  fixed_size_usd: numeric("fixed_size_usd", { precision: 12, scale: 2 }),
  max_position_usd: numeric("max_position_usd", { precision: 12, scale: 2 }),
  min_trade_size_usd: numeric("min_trade_size_usd", {
    precision: 12,
    scale: 2,
  }),
  max_concurrent_positions: integer("max_concurrent_positions"),
  daily_loss_limit_usd: numeric("daily_loss_limit_usd", {
    precision: 12,
    scale: 2,
  }),
  category_whitelist: text("category_whitelist").array(),
  exclude_short_markets: boolean("exclude_short_markets").default(false),
  use_fok_orders: boolean("use_fok_orders").default(true),
});

// ---------------------------------------------------------------------------
// follow_relations — leader → account → template (PRD §6.1)
// ---------------------------------------------------------------------------
export const followRelations = pgTable(
  "follow_relations",
  {
    id: uuid("id").defaultRandom().primaryKey(),
    account_id: uuid("account_id")
      .notNull()
      .references(() => accounts.id),
    leader_address: text("leader_address").notNull(),
    template_id: uuid("template_id")
      .notNull()
      .references(() => copyTemplates.id),
    /** active | paused */
    status: text("status").notNull().default("active"),
    created_at: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    paused_at: timestamp("paused_at", { withTimezone: true }),
    /** Per-leader reconcile cursor (brief §7): last Data-API activity ts. */
    last_seen_ts: timestamp("last_seen_ts", { withTimezone: true }),
  },
  (t) => ({
    statusIdx: index("idx_follow_relations_status").on(t.status),
  })
);

// ---------------------------------------------------------------------------
// leader_trades — cached leader trade history (PRD §6.1)
// ---------------------------------------------------------------------------
export const leaderTrades = pgTable(
  "leader_trades",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    trade_id: text("trade_id").notNull().unique(),
    leader_address: text("leader_address").notNull(),
    market_id: text("market_id"),
    condition_id: text("condition_id"),
    side: text("side"),
    size: numeric("size", { precision: 20, scale: 6 }),
    price: numeric("price", { precision: 10, scale: 6 }),
    timestamp: timestamp("timestamp", { withTimezone: true }),
    /** Cross-source dedup key (brief §3): txHash idempotency anchor. */
    tx_hash: text("tx_hash"),
    raw: jsonb("raw"),
  },
  (t) => ({
    leaderTxUq: unique("uq_leader_trades_leader_tx").on(
      t.leader_address,
      t.tx_hash
    ),
  })
);

// ---------------------------------------------------------------------------
// my_trades — copy trades executed on your account (PRD §6.1)
// ---------------------------------------------------------------------------
export const myTrades = pgTable("my_trades", {
  id: bigserial("id", { mode: "number" }).primaryKey(),
  account_id: uuid("account_id")
    .notNull()
    .references(() => accounts.id),
  leader_address: text("leader_address"),
  copy_of_trade_id: text("copy_of_trade_id"),
  market_id: text("market_id"),
  side: text("side"),
  size: numeric("size", { precision: 20, scale: 6 }),
  price: numeric("price", { precision: 10, scale: 6 }),
  /** pending | filled | rejected | partial */
  fill_status: text("fill_status").notNull().default("pending"),
  pnl_usd: numeric("pnl_usd", { precision: 14, scale: 4 }),
  /** FIFO sell-match accounting (brief §3/§5). */
  matched_qty: numeric("matched_qty", { precision: 20, scale: 6 }),
  remaining_qty: numeric("remaining_qty", { precision: 20, scale: 6 }),
  opened_at: timestamp("opened_at", { withTimezone: true }),
  closed_at: timestamp("closed_at", { withTimezone: true }),
});

// ---------------------------------------------------------------------------
// settings — single-row-per-key config (PRD §6.1)
// ---------------------------------------------------------------------------
export const settings = pgTable("settings", {
  key: text("key").primaryKey(),
  value: jsonb("value"),
  updated_at: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// ---------------------------------------------------------------------------
// backtest_jobs — async backtest queue (PRD §5.4)
// ---------------------------------------------------------------------------
export const backtestJobs = pgTable("backtest_jobs", {
  id: uuid("id").defaultRandom().primaryKey(),
  leader_addresses: text("leader_addresses").array().notNull(),
  template_params: jsonb("template_params"),
  start_date: date("start_date").notNull(), // Postgres DATE — matches SQLAlchemy Date model
  end_date: date("end_date").notNull(),
  initial_capital_usd: numeric("initial_capital_usd", {
    precision: 14,
    scale: 2,
  }).notNull(),
  slippage_bps: integer("slippage_bps").notNull().default(150),
  execution_delay_seconds: integer("execution_delay_seconds")
    .notNull()
    .default(30),
  /** queued | running | completed | failed */
  status: text("status").notNull().default("pending"),
  progress: numeric("progress", { precision: 5, scale: 2 })
    .notNull()
    .default("0"),
  result: jsonb("result"),
  created_at: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// ---------------------------------------------------------------------------
// pending_actions — dangerous op confirmations (PRD §5.7)
// ---------------------------------------------------------------------------
export const pendingActions = pgTable("pending_actions", {
  id: uuid("id").defaultRandom().primaryKey(),
  action_type: text("action_type").notNull(),
  params: jsonb("params"),
  /** awaiting_confirm | confirmed | expired | rejected */
  status: text("status").notNull().default("awaiting_confirm"),
  confirm_passphrase_hash: text("confirm_passphrase_hash"),
  expires_at: timestamp("expires_at", { withTimezone: true }),
  created_at: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// ---------------------------------------------------------------------------
// push_subscriptions — Web Push endpoints (PRD §5.8, Phase 5)
// ---------------------------------------------------------------------------
export const pushSubscriptions = pgTable("push_subscriptions", {
  id: uuid("id").defaultRandom().primaryKey(),
  endpoint: text("endpoint").notNull().unique(),
  p256dh_key: text("p256dh_key").notNull(),
  auth_key: text("auth_key").notNull(),
  /** e.g. "iPhone Safari" */
  device_label: text("device_label"),
  created_at: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  last_used_at: timestamp("last_used_at", { withTimezone: true }),
});

// ---------------------------------------------------------------------------
// events — system events for Web Push sender (PRD §5.8)
// ---------------------------------------------------------------------------
export const events = pgTable("events", {
  id: bigserial("id", { mode: "number" }).primaryKey(),
  event_type: text("event_type").notNull(),
  severity: text("severity").notNull().default("info"),
  payload: jsonb("payload"),
  pushed_at: timestamp("pushed_at", { withTimezone: true }),
  created_at: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// ---------------------------------------------------------------------------
// redeem_jobs — durable claim-winnings queue (PolyHermes brief §9)
// Idempotent on (account_id, condition_id, outcome_index); survives restart
// so a resolved position is never double-redeemed.
// ---------------------------------------------------------------------------
export const redeemJobs = pgTable(
  "redeem_jobs",
  {
    id: uuid("id").defaultRandom().primaryKey(),
    account_id: uuid("account_id")
      .notNull()
      .references(() => accounts.id),
    condition_id: text("condition_id"),
    outcome_index: integer("outcome_index"),
    /**
     * 1 << outcome_index bitmask passed to CTF.redeemPositions (brief §9).
     * bigint (not integer): 1 << outcome_index overflows INT4 for
     * outcome_index >= 31 (NegRisk multi-outcome) → silent insert failure.
     * `mode: "number"` — values stay well within JS safe-integer range.
     */
    index_set: bigint("index_set", { mode: "number" }),
    is_neg_risk: boolean("is_neg_risk"),
    /** pending | submitted | confirmed | failed */
    status: text("status").notNull().default("pending"),
    tx_hash: text("tx_hash"),
    detected_at: timestamp("detected_at", { withTimezone: true }),
    confirmed_at: timestamp("confirmed_at", { withTimezone: true }),
    /**
     * Bounded redeem retry counter (defence-in-depth vs infinite redeem
     * retry). E-core wires the max_attempts cap; this column only stores it.
     */
    attempts: integer("attempts").notNull().default(0),
    created_at: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => ({
    acctCondOutcomeUq: unique("uq_redeem_jobs_account_condition_outcome").on(
      t.account_id,
      t.condition_id,
      t.outcome_index
    ),
    /**
     * 3-way mirror of the SQL/ORM ck_redeem_jobs_index_set: a wrong bitmask
     * must not silently redeem the wrong outcome. NULL-tolerant for
     * not-yet-resolved jobs. drizzle-orm 0.45.x supports check() for pg.
     */
    indexSetCk: check(
      "ck_redeem_jobs_index_set",
      sql`${t.index_set} IS NULL OR ${t.index_set} = (1::BIGINT << ${t.outcome_index})`
    ),
    statusIdx: index("idx_redeem_jobs_status").on(t.status),
  })
);

// ---------------------------------------------------------------------------
// copy_skips — queryable skip/error log w/ reason taxonomy (brief §7)
// ---------------------------------------------------------------------------
export const copySkips = pgTable(
  "copy_skips",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    leader_address: text("leader_address"),
    condition_id: text("condition_id"),
    /** One of polyecho.shared.copytrading.SkipReason (StrEnum value). */
    skip_reason: text("skip_reason").notNull(),
    detail: text("detail"),
    payload: jsonb("payload"),
    created_at: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => ({
    createdAtIdx: index("idx_copy_skips_created_at").on(t.created_at),
  })
);
