/**
 * Drizzle ORM schema — mirrors PRD §6.1 exactly.
 * All tables live in the default "public" schema of the "polyecho" Postgres DB.
 */

import {
  pgTable,
  uuid,
  text,
  date,
  integer,
  boolean,
  jsonb,
  numeric,
  bigserial,
  timestamp,
  primaryKey,
} from "drizzle-orm/pg-core";

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
  /** AES-256-GCM encrypted; held only by trader-core process */
  encrypted_private_key: text("encrypted_private_key"),
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
export const followRelations = pgTable("follow_relations", {
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
});

// ---------------------------------------------------------------------------
// leader_trades — cached leader trade history (PRD §6.1)
// ---------------------------------------------------------------------------
export const leaderTrades = pgTable("leader_trades", {
  id: bigserial("id", { mode: "number" }).primaryKey(),
  trade_id: text("trade_id").notNull().unique(),
  leader_address: text("leader_address").notNull(),
  market_id: text("market_id"),
  condition_id: text("condition_id"),
  side: text("side"),
  size: numeric("size", { precision: 20, scale: 6 }),
  price: numeric("price", { precision: 10, scale: 6 }),
  timestamp: timestamp("timestamp", { withTimezone: true }),
  raw: jsonb("raw"),
});

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
  }),
  slippage_bps: integer("slippage_bps").default(150),
  execution_delay_seconds: integer("execution_delay_seconds").default(30),
  /** queued | running | completed | failed */
  status: text("status").notNull().default("queued"),
  progress: numeric("progress", { precision: 5, scale: 2 }).default("0"),
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
