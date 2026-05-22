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
  /** Phase-3 discovery cursor: high-water mark of the latest Data-API
   * TRADE ts processed (anti-duplication — drives the hourly windowed
   * re-scan + daily staleness gate). */
  last_seen_ts: timestamp("last_seen_ts", { withTimezone: true }),
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
  /** new | watching | following | rejected (operator-review intent) */
  user_status: text("user_status").notNull().default("new"),
  user_notes: text("user_notes"),
  /**
   * C1 paper-trading ladder — AUTOMATED state, kept SEPARATE from
   * user_status. discovered | candidate | paper | trial_ready | cooldown |
   * retired (CHECK enforced in 0000_init.sql + the SQLAlchemy model).
   * Driven by the C1-P3 state machine; this phase only declares it.
   */
  lifecycle_state: text("lifecycle_state").notNull().default("discovered"),
  /** When a cooldown ends; NULL for a non-cooldown leader. */
  cooldown_until: timestamp("cooldown_until", { withTimezone: true }),
  /** How many times this leader has been put on cooldown — 3 retires it. */
  cooldown_count: integer("cooldown_count").notNull().default(0),
  /** Operator override: a locked leader is skipped by the ladder entirely. */
  locked: boolean("locked").notNull().default(false),
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
  /**
   * Tri-mode keyword filter + upper market-end bound (PolyHermes deep-dive
   * B4). `keyword_filter_mode` is disabled | whitelist | blacklist (default
   * disabled so existing templates behave unchanged); `keywords` is the
   * operator term list matched against a market title/category;
   * `market_max_end_seconds` mirrors the lower bound as a ceiling.
   */
  keyword_filter_mode: text("keyword_filter_mode").default("disabled"),
  keywords: text("keywords").array(),
  market_max_end_seconds: integer("market_max_end_seconds"),
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
    /**
     * A3 (PolyHermes deep-dive): durable per-trade copy-attempt outcome
     * (PolyHermes' processed_trade.status slot) — 'success' | 'failed'.
     * Persisted on both the placed and the failed-attempt path so a
     * redelivery of a known-bad trade is not re-attempted. CHECK-
     * constrained in SQL to ('success','failed') or NULL.
     */
    status: text("status"),
    processed_at: timestamp("processed_at", { withTimezone: true }),
  },
  (t) => ({
    leaderTxUq: unique("uq_leader_trades_leader_tx").on(
      t.leader_address,
      t.tx_hash
    ),
  })
);

// ---------------------------------------------------------------------------
// markets — persisted market-metadata cache (C4, PolyHermes deep-dive)
// LiveDataSource hits the data-api / Gamma every cycle for the same market
// metadata with no persistence; this table backs a cache -> DB -> remote
// read layering. A RESOLVED market (is_resolved=true) is immutable — its row
// is served forever with no remote re-fetch; an unresolved market uses a
// short freshness window keyed on updated_at. condition_id is the natural PK.
// ---------------------------------------------------------------------------
export const markets = pgTable(
  "markets",
  {
    condition_id: text("condition_id").primaryKey(),
    /** Polymarket internal market id (gamma "id" / data-api "market_id"). */
    market_id: text("market_id"),
    question: text("question"),
    slug: text("slug"),
    /** NegRisk flag — selects the NegRisk Exchange contract for orders. */
    neg_risk: boolean("neg_risk"),
    /** CLOB price tick size (e.g. 0.01); text to preserve the exact decimal. */
    tick_size: text("tick_size"),
    end_date: timestamp("end_date", { withTimezone: true }),
    /** Outcome token list (token_id / outcome / price dicts). */
    outcomes: jsonb("outcomes"),
    /** Resolved/settled flag — once true the row is immutable. */
    is_resolved: boolean("is_resolved").notNull().default(false),
    /** Settled winning-outcome price (0/1 for binary); NULL until resolved. */
    settled_price: numeric("settled_price", { precision: 10, scale: 6 }),
    raw: jsonb("raw"),
    /** When this row was first cached. */
    cached_at: timestamp("cached_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    /** When this row was last refreshed from a remote read. */
    updated_at: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => ({
    /**
     * The cache layer's opportunistic-refresh sweep selects unresolved rows
     * by staleness; a partial index keeps it to the live subset (3-way
     * aligned with idx_markets_unresolved_updated_at in 0000_init.sql).
     */
    unresolvedUpdatedAtIdx: index("idx_markets_unresolved_updated_at")
      .on(t.updated_at)
      .where(sql`${t.is_resolved} = false`),
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
  /**
   * B1 (PolyHermes deep-dive): denormalised neutral-scenario summary stats,
   * promoted out of the `result` jsonb so the /backtest list page can
   * sort/filter without unpacking a multi-megabyte body. All NULLable: a
   * pending/running/failed job has no summary yet, and `result` stays the
   * source of truth (these are an additive read-optimised mirror).
   */
  total_return_pct: numeric("total_return_pct", { precision: 14, scale: 4 }),
  win_rate: numeric("win_rate", { precision: 6, scale: 4 }),
  max_drawdown_pct: numeric("max_drawdown_pct", { precision: 8, scale: 4 }),
  total_trades: integer("total_trades"),
  sharpe_ratio: numeric("sharpe_ratio", { precision: 10, scale: 4 }),
  total_fees_usd: numeric("total_fees_usd", { precision: 14, scale: 4 }),
  created_at: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// ---------------------------------------------------------------------------
// backtest_trades — per-fill ledger for a backtest job (B1, PolyHermes
// deep-dive). One row per executed fill (BUY / SELL / MTM) carrying the
// equity curve (equity_after) so the /backtest pages render/sort without
// unpacking the result jsonb. PolyHermes' backtest_trade.balance_after.
// ---------------------------------------------------------------------------
export const backtestTrades = pgTable(
  "backtest_trades",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    backtest_job_id: uuid("backtest_job_id")
      .notNull()
      .references(() => backtestJobs.id),
    /** Which leader + scenario this fill belongs to. */
    leader_address: text("leader_address").notNull(),
    /** optimistic | neutral | pessimistic */
    scenario: text("scenario").notNull(),
    /** Monotonic per-(job, leader, scenario) fill index. */
    order_index: integer("order_index").notNull(),
    /** BUY | SELL | MTM */
    side: text("side").notNull(),
    condition_id: text("condition_id"),
    shares: numeric("shares", { precision: 20, scale: 6 }),
    price: numeric("price", { precision: 10, scale: 6 }),
    fee: numeric("fee", { precision: 14, scale: 4 }).notNull().default("0"),
    pnl: numeric("pnl", { precision: 14, scale: 4 }).notNull().default("0"),
    /** Equity (initial_capital + cumulative PnL) AFTER this fill. */
    equity_after: numeric("equity_after", { precision: 16, scale: 4 }),
    ts: timestamp("ts", { withTimezone: true }),
    created_at: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => ({
    jobIdx: index("idx_backtest_trades_job").on(t.backtest_job_id),
  })
);

// ---------------------------------------------------------------------------
// paper_sessions — one forward shadow-copy run of a leader (C1, PolyHermes
// deep-dive). A candidate_leaders row at lifecycle_state='paper' has exactly
// one open paper_session. The denormalised summary columns are recomputed
// each paper-worker cycle and gated on by the ladder state machine (C1-P3).
// cursor_ts is the high-water mark of the last simulated leader-trade ts.
// ---------------------------------------------------------------------------
export const paperSessions = pgTable(
  "paper_sessions",
  {
    id: uuid("id").defaultRandom().primaryKey(),
    leader_address: text("leader_address").notNull(),
    /** open | trial_ready | cooldown | retired */
    state: text("state").notNull().default("open"),
    started_at: timestamp("started_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    /** Simulated starting paper capital for this session. */
    capital_usd: numeric("capital_usd", { precision: 14, scale: 2 })
      .notNull()
      .default("0"),
    /** Denormalised summary — recomputed each cycle, gated on by the ladder. */
    score: numeric("score", { precision: 6, scale: 2 }),
    trade_count: integer("trade_count").notNull().default(0),
    copyable_pnl: numeric("copyable_pnl", { precision: 16, scale: 4 }),
    max_drawdown: numeric("max_drawdown", { precision: 8, scale: 4 }),
    unknown_exposure_ratio: numeric("unknown_exposure_ratio", {
      precision: 6,
      scale: 4,
    }),
    filtered_ratio: numeric("filtered_ratio", { precision: 6, scale: 4 }),
    /** High-water mark of the last simulated leader-trade timestamp. */
    cursor_ts: timestamp("cursor_ts", { withTimezone: true }),
    created_at: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    updated_at: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => ({
    leaderIdx: index("idx_paper_sessions_leader").on(t.leader_address),
  })
);

// ---------------------------------------------------------------------------
// paper_trades — per-fill ledger for a paper_session (C1, PolyHermes
// deep-dive). The paper-trading analogue of backtest_trades: each simulated
// fill (BUY / SELL / MTM) is its own row carrying the running paper balance
// (balance_after). Column shape/types mirror backtest_trades.
// ---------------------------------------------------------------------------
export const paperTrades = pgTable(
  "paper_trades",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    session_id: uuid("session_id")
      .notNull()
      .references(() => paperSessions.id),
    /** BUY | SELL | MTM */
    side: text("side").notNull(),
    condition_id: text("condition_id"),
    shares: numeric("shares", { precision: 20, scale: 6 }),
    price: numeric("price", { precision: 10, scale: 6 }),
    fee: numeric("fee", { precision: 14, scale: 4 }).notNull().default("0"),
    pnl: numeric("pnl", { precision: 14, scale: 4 }).notNull().default("0"),
    /** Paper balance (capital + cumulative PnL) AFTER this fill. */
    balance_after: numeric("balance_after", { precision: 16, scale: 4 }),
    ts: timestamp("ts", { withTimezone: true }),
  },
  (t) => ({
    sessionIdx: index("idx_paper_trades_session").on(t.session_id),
  })
);

// ---------------------------------------------------------------------------
// paper_positions — the open FIFO lots of a paper_session (C1, PolyHermes
// deep-dive). One row per open (session, condition_id). lots is a jsonb array
// of open FIFO lots, each {shares, fill_price, opened_at}; a single-row jsonb
// array (vs one row per lot) lets the worker rewrite the whole list atomically
// and rebuild all open state after a restart with one indexed SELECT.
// ---------------------------------------------------------------------------
export const paperPositions = pgTable(
  "paper_positions",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    session_id: uuid("session_id")
      .notNull()
      .references(() => paperSessions.id),
    condition_id: text("condition_id"),
    /** Open FIFO lots — jsonb array of {shares, fill_price, opened_at}. */
    lots: jsonb("lots")
      .notNull()
      .default(sql`'[]'::jsonb`),
    updated_at: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (t) => ({
    sessionIdx: index("idx_paper_positions_session").on(t.session_id),
  })
);

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
