-- =============================================================================
-- PolyEcho — Phase 1 baseline schema
-- File: infra/migrations/0000_init.sql
--
-- IMPORTANT: The canonical schema generator going forward is:
--   pnpm db:generate   (drizzle-kit, run in-container or via pnpm workspace)
-- This file is the reviewable Phase 1 baseline applied via:
--   polyecho db migrate   OR   scripts/dev.sh migrate
--
-- All tables mirror packages/web/lib/db/schema.ts exactly.
-- Use CREATE TABLE IF NOT EXISTS for idempotency on re-apply.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- accounts — your own Polymarket accounts (PRD §5.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
  id                   UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  name                 TEXT        NOT NULL,
  eoa_address          TEXT        NOT NULL UNIQUE,
  funder_address       TEXT,
  signature_type       INTEGER,
  -- AES-256-GCM encrypted; held only by trader-core process
  encrypted_private_key TEXT,
  -- pending_key_import | ready | disabled
  status               TEXT        NOT NULL DEFAULT 'pending_key_import',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- candidate_leaders — discovery pool (PRD §5.2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidate_leaders (
  wallet_address       TEXT        NOT NULL PRIMARY KEY,
  display_name         TEXT,
  sources              TEXT[],
  first_discovered_at  TIMESTAMPTZ,
  last_active_at       TIMESTAMPTZ,
  stats_7d             JSONB,
  stats_30d            JSONB,
  stats_90d            JSONB,
  frequency_stats      JSONB,
  strategy_type        TEXT,
  backtest_default     JSONB,
  profit_score         NUMERIC(6,2),
  replicability_score  NUMERIC(6,2),
  total_score          NUMERIC(6,2),
  risk_flags           TEXT[],
  specialization       TEXT[],
  -- new | watching | following | rejected
  user_status          TEXT        NOT NULL DEFAULT 'new',
  user_notes           TEXT,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- candidate_score_history — daily snapshot (PRD §6.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidate_score_history (
  wallet_address  TEXT        NOT NULL,
  date            TEXT        NOT NULL, -- ISO date string YYYY-MM-DD
  total_score     NUMERIC(6,2),
  rank            INTEGER,
  stats           JSONB,
  PRIMARY KEY (wallet_address, date)
);

-- ---------------------------------------------------------------------------
-- copy_templates — risk & sizing presets (PRD §5.6)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS copy_templates (
  id                      UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  name                    TEXT        NOT NULL,
  copy_ratio              NUMERIC(8,5),
  fixed_size_usd          NUMERIC(12,2),
  max_position_usd        NUMERIC(12,2),
  min_trade_size_usd      NUMERIC(12,2),
  max_concurrent_positions INTEGER,
  daily_loss_limit_usd    NUMERIC(12,2),
  category_whitelist      TEXT[],
  exclude_short_markets   BOOLEAN     DEFAULT false,
  use_fok_orders          BOOLEAN     DEFAULT true
);

-- ---------------------------------------------------------------------------
-- follow_relations — leader → account → template (PRD §6.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS follow_relations (
  id             UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  account_id     UUID        NOT NULL REFERENCES accounts(id),
  leader_address TEXT        NOT NULL,
  template_id    UUID        NOT NULL REFERENCES copy_templates(id),
  -- active | paused
  status         TEXT        NOT NULL DEFAULT 'active',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  paused_at      TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- leader_trades — cached leader trade history (PRD §6.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leader_trades (
  id             BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trade_id       TEXT        NOT NULL UNIQUE,
  leader_address TEXT        NOT NULL,
  market_id      TEXT,
  condition_id   TEXT,
  side           TEXT,
  size           NUMERIC(20,6),
  price          NUMERIC(10,6),
  timestamp      TIMESTAMPTZ,
  raw            JSONB
);

-- ---------------------------------------------------------------------------
-- my_trades — copy trades executed on your account (PRD §6.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS my_trades (
  id               BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id       UUID        NOT NULL REFERENCES accounts(id),
  leader_address   TEXT,
  copy_of_trade_id TEXT,
  market_id        TEXT,
  side             TEXT,
  size             NUMERIC(20,6),
  price            NUMERIC(10,6),
  -- pending | filled | rejected | partial
  fill_status      TEXT        NOT NULL DEFAULT 'pending',
  pnl_usd          NUMERIC(14,4),
  opened_at        TIMESTAMPTZ,
  closed_at        TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- settings — single-row-per-key config (PRD §6.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT        NOT NULL PRIMARY KEY,
  value      JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- backtest_jobs — async backtest queue (PRD §5.4)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_jobs (
  id                       UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  leader_addresses         TEXT[]      NOT NULL,
  template_params          JSONB,
  start_date               TEXT        NOT NULL, -- ISO date string
  end_date                 TEXT        NOT NULL,
  initial_capital_usd      NUMERIC(14,2),
  slippage_bps             INTEGER     DEFAULT 150,
  execution_delay_seconds  INTEGER     DEFAULT 30,
  -- queued | running | completed | failed
  status                   TEXT        NOT NULL DEFAULT 'queued',
  progress                 NUMERIC(5,2) DEFAULT 0,
  result                   JSONB,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- pending_actions — dangerous op confirmations (PRD §5.7)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_actions (
  id                      UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  action_type             TEXT        NOT NULL,
  params                  JSONB,
  -- awaiting_confirm | confirmed | expired | rejected
  status                  TEXT        NOT NULL DEFAULT 'awaiting_confirm',
  confirm_passphrase_hash TEXT,
  expires_at              TIMESTAMPTZ,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- push_subscriptions — Web Push endpoints (PRD §5.8)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id           UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  endpoint     TEXT        NOT NULL UNIQUE,
  p256dh_key   TEXT        NOT NULL,
  auth_key     TEXT        NOT NULL,
  device_label TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- events — system events for Web Push sender (PRD §5.8)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  id          BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_type  TEXT        NOT NULL,
  severity    TEXT        NOT NULL DEFAULT 'info',
  payload     JSONB,
  pushed_at   TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- Indexes — hot query paths
-- =============================================================================

-- candidate_leaders: leaderboard sort, status filter, strategy filter
CREATE INDEX IF NOT EXISTS idx_candidate_leaders_total_score
  ON candidate_leaders (total_score DESC);

CREATE INDEX IF NOT EXISTS idx_candidate_leaders_user_status
  ON candidate_leaders (user_status);

CREATE INDEX IF NOT EXISTS idx_candidate_leaders_strategy_type
  ON candidate_leaders (strategy_type);

-- candidate_score_history: time-series lookup per wallet
CREATE INDEX IF NOT EXISTS idx_candidate_score_history_wallet_date
  ON candidate_score_history (wallet_address, date);

-- leader_trades: per-leader chronological scan
CREATE INDEX IF NOT EXISTS idx_leader_trades_leader_timestamp
  ON leader_trades (leader_address, timestamp);

-- my_trades: per-account chronological scan
CREATE INDEX IF NOT EXISTS idx_my_trades_account_opened_at
  ON my_trades (account_id, opened_at);

-- events: push-sender polls for un-pushed events
CREATE INDEX IF NOT EXISTS idx_events_pushed_at_null
  ON events (pushed_at)
  WHERE pushed_at IS NULL;

-- pending_actions: expiry sweep
CREATE INDEX IF NOT EXISTS idx_pending_actions_status_expires_at
  ON pending_actions (status, expires_at);
