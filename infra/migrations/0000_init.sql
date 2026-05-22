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
  -- Practical wallet identity (brief §6/§9): eoa | magic | safe | poly_1271.
  -- Proxy/funder is the on-chain identity; this makes the mode explicit
  -- when not derivable from signature_type. resolve_wallet() fails closed
  -- on unknown.
  wallet_mode          TEXT,
  -- AES-256-GCM ciphertext (raw bytes); held only by trader-core process.
  -- BYTEA (not TEXT) so the binary ciphertext is stored losslessly and
  -- matches the SQLAlchemy LargeBinary ORM column + Drizzle bytea().
  encrypted_private_key BYTEA,
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
  -- Phase-3 discovery cursor: high-water mark of the latest Data-API
  -- TRADE timestamp processed for this wallet. Drives the hourly
  -- large-new-position windowed re-scan (start=last_seen_ts) and the
  -- daily full-scoring staleness gate so a wallet is not re-fetched /
  -- re-scored when nothing changed (anti-duplication). NULLable: a
  -- never-yet-scanned candidate legitimately has no cursor.
  last_seen_ts         TIMESTAMPTZ,
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

-- Idempotent forward-fill for DBs created before the Phase-3
-- last_seen_ts discovery cursor existed. CREATE TABLE IF NOT EXISTS
-- above is a no-op on a warm DB, so this guarded ADD COLUMN brings an
-- already-existing candidate_leaders up to the current shape. NULLable
-- (no backfill value) — a never-yet-scanned candidate has no cursor;
-- ADD COLUMN IF NOT EXISTS makes re-apply a no-op.
ALTER TABLE candidate_leaders
  ADD COLUMN IF NOT EXISTS last_seen_ts TIMESTAMPTZ;

-- Idempotent forward-fill for DBs created before the C1 paper-trading
-- ladder columns existed. CREATE TABLE IF NOT EXISTS above is a no-op on
-- a warm DB, so these guarded statements bring an already-existing
-- candidate_leaders up to the current shape. Each leg is individually
-- idempotent (ADD COLUMN IF NOT EXISTS / pg_constraint guard) so a
-- warm-volume redeploy never fails — same pattern as the
-- redeem_jobs.attempts / leader_trades.status / backtest_jobs blocks.
-- lifecycle_state is the AUTOMATED ladder state, kept SEPARATE from
-- user_status (the operator-review intent — untouched here). The NOT NULL
-- DEFAULT 'discovered' / DEFAULT 0 / DEFAULT false backfill existing rows.
DO $$
BEGIN
  -- lifecycle_state column add (NOT NULL DEFAULT 'discovered' backfills).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'candidate_leaders'
      AND column_name = 'lifecycle_state'
  ) THEN
    ALTER TABLE candidate_leaders
      ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'discovered';
  END IF;

  -- cooldown_until column add (NULLable — a non-cooldown leader has none).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'candidate_leaders'
      AND column_name = 'cooldown_until'
  ) THEN
    ALTER TABLE candidate_leaders ADD COLUMN cooldown_until TIMESTAMPTZ;
  END IF;

  -- cooldown_count column add (NOT NULL DEFAULT 0 backfills existing rows).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'candidate_leaders'
      AND column_name = 'cooldown_count'
  ) THEN
    ALTER TABLE candidate_leaders
      ADD COLUMN cooldown_count INTEGER NOT NULL DEFAULT 0;
  END IF;

  -- locked column add (NOT NULL DEFAULT false backfills existing rows).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'candidate_leaders'
      AND column_name = 'locked'
  ) THEN
    ALTER TABLE candidate_leaders
      ADD COLUMN locked BOOLEAN NOT NULL DEFAULT false;
  END IF;

  -- lifecycle_state CHECK add (guarded by pg_constraint so re-apply is a
  -- no-op): the automated ladder state is one of the six known values.
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_candidate_leaders_lifecycle_state'
      AND conrelid = 'public.candidate_leaders'::regclass
  ) THEN
    ALTER TABLE candidate_leaders
      ADD CONSTRAINT ck_candidate_leaders_lifecycle_state
      CHECK (lifecycle_state IN ('discovered', 'candidate', 'paper',
                                 'trial_ready', 'cooldown', 'retired'));
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- candidate_score_history — daily snapshot (PRD §6.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidate_score_history (
  wallet_address  TEXT        NOT NULL,
  date            DATE        NOT NULL, -- Postgres DATE — matches SQLAlchemy Date model
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
  use_fok_orders          BOOLEAN     DEFAULT true,
  -- Tri-mode keyword filter + upper market-end bound (PolyHermes deep-dive
  -- B4). keyword_filter_mode is disabled | whitelist | blacklist; keywords
  -- is the operator term list matched against a market title/category.
  -- market_max_end_seconds mirrors the existing lower bound as a ceiling.
  keyword_filter_mode     TEXT        DEFAULT 'disabled',
  keywords                TEXT[],
  market_max_end_seconds  INTEGER
);

-- Idempotent forward-fill for DBs created before the B4 keyword-filter /
-- upper-end-bound columns existed. CREATE TABLE IF NOT EXISTS above is a
-- no-op on a warm DB, so these guarded ADD COLUMNs bring an already-existing
-- copy_templates up to the current shape — same warm-volume-safe pattern as
-- the redeem_jobs.attempts / leader_trades.status / backtest_jobs blocks.
ALTER TABLE copy_templates
  ADD COLUMN IF NOT EXISTS keyword_filter_mode    TEXT DEFAULT 'disabled';
ALTER TABLE copy_templates
  ADD COLUMN IF NOT EXISTS keywords               TEXT[];
ALTER TABLE copy_templates
  ADD COLUMN IF NOT EXISTS market_max_end_seconds INTEGER;

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
  paused_at      TIMESTAMPTZ,
  -- Per-leader reconcile cursor (brief §7): last Data-API activity ts
  -- reconciled, so a WS gap / restart backfills missed trades.
  last_seen_ts   TIMESTAMPTZ
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
  -- Cross-source dedup key (brief §3): txHash is THE idempotency anchor
  -- across the WS-user-channel path and the on-chain RPC cross-check.
  tx_hash        TEXT,
  raw            JSONB,
  -- A3 (PolyHermes deep-dive): durable per-trade copy-attempt outcome
  -- (PolyHermes' processed_trade.status slot). 'success' = the copy order
  -- was placed; 'failed' = a copy attempt was made but did not place
  -- (retries exhausted / executor reject). Persisted on BOTH paths so a
  -- WS/poll redelivery of a known-bad trade is not re-attempted in a loop.
  -- NULLable: a leader-trade row written by a future pure-cache path (no
  -- copy attempt) legitimately has no outcome yet.
  status         TEXT,
  -- When the copy attempt outcome (success/failed) was recorded.
  processed_at   TIMESTAMPTZ,
  CONSTRAINT uq_leader_trades_leader_tx UNIQUE (leader_address, tx_hash),
  -- Defence-in-depth: only the two known outcomes (or NULL) are storable.
  CONSTRAINT ck_leader_trades_status
    CHECK (status IS NULL OR status IN ('success', 'failed'))
);

-- Idempotent forward-fill for DBs created before the A3 status /
-- processed_at columns + the status CHECK existed. CREATE TABLE IF NOT
-- EXISTS above is a no-op on a warm DB, so these guarded statements bring
-- an already-existing leader_trades up to the current shape. Each leg is
-- individually idempotent (ADD COLUMN IF NOT EXISTS / pg_constraint guard)
-- so a warm-volume redeploy never fails.
DO $$
BEGIN
  -- status column add (NULLable — no backfill value; pre-A3 rows have no
  -- recorded copy-attempt outcome).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'leader_trades'
      AND column_name = 'status'
  ) THEN
    ALTER TABLE leader_trades ADD COLUMN status TEXT;
  END IF;

  -- processed_at column add (NULLable).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'leader_trades'
      AND column_name = 'processed_at'
  ) THEN
    ALTER TABLE leader_trades ADD COLUMN processed_at TIMESTAMPTZ;
  END IF;

  -- status CHECK add (guarded by pg_constraint so re-apply is a no-op).
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_leader_trades_status'
      AND conrelid = 'public.leader_trades'::regclass
  ) THEN
    ALTER TABLE leader_trades
      ADD CONSTRAINT ck_leader_trades_status
      CHECK (status IS NULL OR status IN ('success', 'failed'));
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- markets — persisted market-metadata cache (C4, PolyHermes deep-dive)
-- LiveDataSource hits the data-api / Gamma every cycle for the same market
-- metadata with no persistence; this table backs a cache -> DB -> remote
-- read layering. A RESOLVED market (is_resolved=true) is immutable — its
-- row is served forever with no remote re-fetch; an unresolved market uses
-- a short freshness window keyed on updated_at. condition_id is the natural
-- PK (THE market identifier shared across the activity/positions APIs).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
  condition_id   TEXT        NOT NULL PRIMARY KEY,
  -- Polymarket internal market id (gamma "id" / data-api "market_id").
  market_id      TEXT,
  question       TEXT,
  slug           TEXT,
  -- NegRisk flag — selects the NegRisk Exchange contract for orders.
  neg_risk       BOOLEAN,
  -- CLOB price tick size (e.g. 0.01); TEXT to preserve the exact decimal
  -- string the API returns.
  tick_size      TEXT,
  end_date       TIMESTAMPTZ,
  -- Outcome token list (token_id / outcome / price dicts).
  outcomes       JSONB,
  -- Resolved/settled flag — once true the row is immutable (cached forever,
  -- no remote re-fetch).
  is_resolved    BOOLEAN     NOT NULL DEFAULT false,
  -- Settled winning-outcome price (0/1 for binary); NULL until resolved.
  settled_price  NUMERIC(10,6),
  raw            JSONB,
  -- When this row was first cached.
  cached_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- When this row was last refreshed from a remote read — the unresolved
  -- freshness window is measured against this.
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
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
  -- FIFO sell-match accounting (brief §3/§5): matched_qty = qty consumed
  -- by later sells; remaining_qty = unmatched BUY qty for FIFO exits.
  matched_qty      NUMERIC(20,6),
  remaining_qty    NUMERIC(20,6),
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
  start_date               DATE        NOT NULL,
  end_date                 DATE        NOT NULL,
  initial_capital_usd      NUMERIC(14,2) NOT NULL,
  slippage_bps             INTEGER     NOT NULL DEFAULT 150,
  execution_delay_seconds  INTEGER     NOT NULL DEFAULT 30,
  -- queued | running | completed | failed
  status                   TEXT        NOT NULL DEFAULT 'pending',
  progress                 NUMERIC(5,2) NOT NULL DEFAULT 0,
  result                   JSONB,
  -- B1 (PolyHermes deep-dive): denormalised neutral-scenario summary stats,
  -- promoted out of the result JSONB so the /backtest list page can
  -- sort/filter without unpacking a multi-megabyte body. All NULLable: a
  -- pending/running/failed job has no summary yet, and the JSONB result
  -- stays the source of truth (these are an additive read-optimised mirror).
  total_return_pct         NUMERIC(14,4),
  win_rate                 NUMERIC(6,4),
  max_drawdown_pct         NUMERIC(8,4),
  total_trades             INTEGER,
  sharpe_ratio             NUMERIC(10,4),
  total_fees_usd           NUMERIC(14,4),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent forward-fill for DBs created before the B1 denormalised
-- summary columns existed. CREATE TABLE IF NOT EXISTS above is a no-op on a
-- warm DB, so these guarded ADD COLUMNs bring an already-existing
-- backtest_jobs up to the current shape. Each is individually idempotent
-- (ADD COLUMN IF NOT EXISTS) so a warm-volume redeploy never fails — same
-- pattern as the redeem_jobs.attempts / leader_trades.status blocks above.
ALTER TABLE backtest_jobs
  ADD COLUMN IF NOT EXISTS total_return_pct   NUMERIC(14,4);
ALTER TABLE backtest_jobs
  ADD COLUMN IF NOT EXISTS win_rate           NUMERIC(6,4);
ALTER TABLE backtest_jobs
  ADD COLUMN IF NOT EXISTS max_drawdown_pct   NUMERIC(8,4);
ALTER TABLE backtest_jobs
  ADD COLUMN IF NOT EXISTS total_trades       INTEGER;
ALTER TABLE backtest_jobs
  ADD COLUMN IF NOT EXISTS sharpe_ratio       NUMERIC(10,4);
ALTER TABLE backtest_jobs
  ADD COLUMN IF NOT EXISTS total_fees_usd     NUMERIC(14,4);

-- ---------------------------------------------------------------------------
-- backtest_trades — per-fill ledger for a backtest job (B1, PolyHermes
-- deep-dive). One row per executed fill (BUY / SELL / MTM) of the neutral or
-- any other scenario, carrying the equity curve (equity_after) so the
-- /backtest detail + list pages can render/sort without unpacking the
-- result JSONB. PolyHermes' backtest_trade.balance_after equivalent. The
-- result JSONB still carries the full trade_log (additive, unchanged).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_trades (
  id               BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  backtest_job_id  UUID        NOT NULL REFERENCES backtest_jobs(id),
  -- Which leader + scenario this fill belongs to (a job runs N leaders ×
  -- up to 3 scenarios). scenario ∈ optimistic | neutral | pessimistic.
  leader_address   TEXT        NOT NULL,
  scenario         TEXT        NOT NULL,
  -- Monotonic per-(job,leader,scenario) fill index — preserves trade_log
  -- order for the equity curve without depending on timestamp ties.
  order_index      INTEGER     NOT NULL,
  -- BUY | SELL | MTM (mark-to-market settlement row).
  side             TEXT        NOT NULL,
  condition_id     TEXT,
  shares           NUMERIC(20,6),
  price            NUMERIC(10,6),
  fee              NUMERIC(14,4) NOT NULL DEFAULT 0,
  pnl              NUMERIC(14,4) NOT NULL DEFAULT 0,
  -- Equity (initial_capital + cumulative PnL) AFTER this fill — the curve.
  equity_after     NUMERIC(16,4),
  ts               TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- paper_sessions — one forward shadow-copy run of a leader (C1, PolyHermes
-- deep-dive). A candidate_leaders row at lifecycle_state='paper' has exactly
-- one open paper_session. The denormalised summary columns (score /
-- trade_count / copyable_pnl / max_drawdown / unknown_exposure_ratio /
-- filtered_ratio) are recomputed each paper-worker cycle and gated on by the
-- ladder state machine (C1-P3). cursor_ts is the high-water mark of the last
-- simulated leader-trade timestamp — the anti-duplication anchor a worker
-- restart resumes from. All summary columns NULLable: a fresh session has no
-- stats yet.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_sessions (
  id                     UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  leader_address         TEXT        NOT NULL,
  -- open | trial_ready | cooldown | retired (session lifecycle).
  state                  TEXT        NOT NULL DEFAULT 'open',
  started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Simulated starting paper capital for this session.
  capital_usd            NUMERIC(14,2) NOT NULL DEFAULT 0,
  -- Denormalised summary — recomputed each cycle, gated on by the ladder.
  score                  NUMERIC(6,2),
  trade_count            INTEGER     NOT NULL DEFAULT 0,
  copyable_pnl           NUMERIC(16,4),
  max_drawdown           NUMERIC(8,4),
  unknown_exposure_ratio NUMERIC(6,4),
  filtered_ratio         NUMERIC(6,4),
  -- High-water mark of the last simulated leader-trade timestamp.
  cursor_ts              TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- paper_trades — per-fill ledger for a paper_session (C1, PolyHermes
-- deep-dive). The paper-trading analogue of backtest_trades: each simulated
-- fill (BUY / SELL / MTM) is its own row carrying the running paper balance
-- (balance_after). The column shape/types mirror backtest_trades because the
-- per-trade simulation math (slippage / fee / FIFO PnL) is reused verbatim.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_trades (
  id            BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id    UUID        NOT NULL REFERENCES paper_sessions(id),
  -- BUY | SELL | MTM (mark-to-market settlement row).
  side          TEXT        NOT NULL,
  condition_id  TEXT,
  shares        NUMERIC(20,6),
  price         NUMERIC(10,6),
  fee           NUMERIC(14,4) NOT NULL DEFAULT 0,
  pnl           NUMERIC(14,4) NOT NULL DEFAULT 0,
  -- Paper balance (capital + cumulative PnL) AFTER this fill — the curve.
  balance_after NUMERIC(16,4),
  ts            TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- paper_positions — the open FIFO lots of a paper_session (C1, PolyHermes
-- deep-dive). One row per open (session, condition_id). lots is a JSONB array
-- of open FIFO lots, each {shares, fill_price, opened_at}. A single-row JSONB
-- array (vs one row per lot) is the cleaner shape: the FIFO discipline is
-- positional, the worker rewrites the whole list atomically when a fill
-- consumes / appends lots, and rebuilding all open state after a restart is
-- one indexed SELECT ... WHERE session_id = ?. NOT NULL DEFAULT '[]' so a
-- fresh position is a valid empty lot list with no NULL special-case.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_positions (
  id           BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id   UUID        NOT NULL REFERENCES paper_sessions(id),
  condition_id TEXT,
  -- Open FIFO lots — JSONB array of {shares, fill_price, opened_at}.
  lots         JSONB       NOT NULL DEFAULT '[]'::jsonb,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
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

-- ---------------------------------------------------------------------------
-- redeem_jobs — durable claim-winnings queue (PolyHermes brief §9)
-- Idempotent on (account_id, condition_id, outcome_index); survives restart
-- so a resolved position is never double-redeemed (improves on PolyHermes'
-- in-memory-only redeem dedup).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS redeem_jobs (
  id            UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  account_id    UUID        NOT NULL REFERENCES accounts(id),
  condition_id  TEXT,
  outcome_index INTEGER,
  -- 1 << outcome_index bitmask passed to CTF.redeemPositions (brief §9).
  -- BIGINT (not INTEGER): index_set = 1 << outcome_index overflows INT4 for
  -- outcome_index >= 31 (NegRisk multi-outcome) → silent insert failure /
  -- lost redeem job. INT8 covers the full Solidity-relevant range.
  index_set     BIGINT,
  is_neg_risk   BOOLEAN,
  -- pending | submitted | confirmed | failed
  status        TEXT        NOT NULL DEFAULT 'pending',
  tx_hash       TEXT,
  detected_at   TIMESTAMPTZ,
  confirmed_at  TIMESTAMPTZ,
  -- Bounded redeem retry counter (defence-in-depth vs infinite redeem
  -- retry). E-core wires the max_attempts cap; this column only stores it.
  attempts      INTEGER     NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_redeem_jobs_account_condition_outcome
    UNIQUE (account_id, condition_id, outcome_index),
  -- Defence-in-depth: a wrong bitmask must not silently redeem the wrong
  -- outcome. index_set_for() already computes 1 << outcome_index in Python;
  -- this CHECK enforces it at the DB. NULL-tolerant: a not-yet-resolved job
  -- legitimately has NULL index_set/outcome_index.
  CONSTRAINT ck_redeem_jobs_index_set
    CHECK (index_set IS NULL OR index_set = (1::BIGINT << outcome_index))
);

-- Idempotent forward-fill for DBs created before index_set→BIGINT, the
-- `attempts` column, and the index_set CHECK existed. CREATE TABLE IF NOT
-- EXISTS above is a no-op on a warm DB, so these guarded statements bring an
-- already-existing redeem_jobs up to the current shape. All are
-- individually idempotent (IF NOT EXISTS / pg_constraint guard / type-noop).
DO $$
BEGIN
  -- index_set INTEGER → BIGINT (no-op if already BIGINT; safe widening).
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'redeem_jobs'
      AND column_name = 'index_set'
      AND data_type = 'integer'
  ) THEN
    ALTER TABLE redeem_jobs ALTER COLUMN index_set TYPE BIGINT;
  END IF;

  -- attempts column add (NOT NULL DEFAULT 0 backfills existing rows).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'redeem_jobs'
      AND column_name = 'attempts'
  ) THEN
    ALTER TABLE redeem_jobs
      ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
  END IF;

  -- index_set CHECK add (guarded by pg_constraint so re-apply is a no-op).
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_redeem_jobs_index_set'
      AND conrelid = 'public.redeem_jobs'::regclass
  ) THEN
    ALTER TABLE redeem_jobs
      ADD CONSTRAINT ck_redeem_jobs_index_set
      CHECK (index_set IS NULL OR index_set = (1::BIGINT << outcome_index));
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- copy_skips — queryable skip/error log w/ reason taxonomy (brief §7)
-- PolyHermes only surfaces failures via Telegram (ephemeral); PolyEcho
-- persists every skip/reject with a stable SkipReason for historical "why".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS copy_skips (
  id             BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  leader_address TEXT,
  condition_id   TEXT,
  -- One of polyecho.shared.copytrading.SkipReason (StrEnum value).
  skip_reason    TEXT        NOT NULL,
  detail         TEXT,
  payload        JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
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

-- follow_relations: trader-core scans active relations on every tick (brief §7)
CREATE INDEX IF NOT EXISTS idx_follow_relations_status
  ON follow_relations (status);

-- redeem_jobs: worker polls pending/submitted jobs (brief §9)
CREATE INDEX IF NOT EXISTS idx_redeem_jobs_status
  ON redeem_jobs (status);

-- copy_skips: dashboard/CLI queries recent skips chronologically (brief §7)
CREATE INDEX IF NOT EXISTS idx_copy_skips_created_at
  ON copy_skips (created_at);

-- backtest_trades: the detail page loads every fill for one job (B1)
CREATE INDEX IF NOT EXISTS idx_backtest_trades_job
  ON backtest_trades (backtest_job_id);

-- paper_sessions: the paper worker scans sessions by leader each cycle (C1)
CREATE INDEX IF NOT EXISTS idx_paper_sessions_leader
  ON paper_sessions (leader_address);

-- paper_trades: the /research detail page loads every fill for one session (C1)
CREATE INDEX IF NOT EXISTS idx_paper_trades_session
  ON paper_trades (session_id);

-- paper_positions: the worker rebuilds open state by session on restart (C1)
CREATE INDEX IF NOT EXISTS idx_paper_positions_session
  ON paper_positions (session_id);

-- markets: the cache layer's opportunistic-refresh sweep selects unresolved
-- rows by staleness (C4); a partial index keeps it to the live (unresolved)
-- subset since resolved rows never need re-fetching.
CREATE INDEX IF NOT EXISTS idx_markets_unresolved_updated_at
  ON markets (updated_at)
  WHERE is_resolved = false;
