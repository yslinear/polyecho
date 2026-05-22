# PolyHermes Deep-Dive — 16-Facet Study

> Source: read-only study of [`WrBug/PolyHermes`](https://github.com/WrBug/PolyHermes)
> (Kotlin/Spring Boot, MySQL, 42 Flyway migrations), by a 16-agent research
> swarm (2026-05-22). Follows the earlier architecture-level
> [`polyhermes-analysis.md`](polyhermes-analysis.md) (5-agent, 2026-05-18)
> and goes deeper on **backtest, leader evaluation, and Polymarket call
> patterns**.
>
> Their stack is JVM/Spring/MySQL; ours is Python 3.12 asyncio / SQLAlchemy
> 2.0 / py-clob-client-v2 / Postgres. Adopt the *patterns*, not the code.
> PolyEcho has already shipped the PRD 0-5 roadmap + R21-R31 hardening;
> this study is for *incremental* learning.

## Headline

PolyEcho is ahead of PolyHermes on most facets — the 2026-05-18 decisions
held up. Confirmed PolyEcho-is-better: SDK-based signing (PolyHermes
hand-rolls EIP-712/keccak/HMAC with 4 correctness traps), backtest realism
(PolyHermes models **no** slippage/fee/delay — its results are
systematically over-optimistic), the 16-step risk gate (superset of
PolyHermes' realised-loss-only gate), multiplicative dual-dimension leader
scoring + normalisation + strategy classification, all 4 signature types
incl. POLY_1271 (PolyHermes has only 1/2 and fail-opens `null→SAFE`),
N-confirmation reorg guard, durable `redeem_jobs` + `wait_receipt`,
structured JSON logging + durable `events` + 8-probe health.

The genuine learnings are below, tiered by effort.

## Per-facet findings (16 angles)

1. **Backtest engine** — PolyHermes DOES backtest (`service/backtest/`,
   ~1485 LOC). Replays Data-API `getUserActivity` chronologically, reuses
   the live filter pipeline. Fills at the leader's exact historical price:
   **no slippage, no fee, no latency** (all hardcoded zero). PolyEcho's
   3-scenario slippage/fee/delay model is materially better; PolyHermes'
   one win is **timestamp-cursor pagination** (no offset ceiling).

2. **Leader scoring** — `research-copyability-v1`: additive 0-100 sum of
   11 fixed-cap components, scored on its *own paper-trades* (forward
   tested, not leaderboard history). Sample-size hard-cap (`<10 trades`
   capped at 59, below the 60 promotion gate). Lifecycle state machine
   CANDIDATE→PAPER→TRIAL_READY→COOLDOWN→RETIRED.

3. **CLOB orders** — FAK for copy/market, GTC for limit; hand-rolled
   rounding with **hardcoded `tickSize=0.01`**; negRisk Exchange-contract
   selection; per-attempt re-sign with fresh salt; out-of-band fill
   tracking via a user-channel WS + `GET /data/order/{id}`.

4. **Signing/auth** — hand-rolled EIP-712 (web3j + BouncyCastle). Traps:
   L2 HMAC ignores server-time; `v` not normalised to 27/28; `encodeAddress`
   silently mis-pads short addresses; homegrown `now*1000+seq` salt.
   derive-first/create-fallback API-key flow is sound.

5. **Market data** — Gamma + CLOB + RPC. Two-tier Caffeine cache:
   metadata LRU(200) with cache→DB(`markets`)→Gamma layering;
   settled-price cache (no expiry, immutable). 30s background backfill
   poller. No `tickSize` storage.

6. **Risk controls** — two-stage gate (`checkFilters` + `checkRiskControls`).
   Caps on **realised loss only** (the dangerous gap). Fail-closed on
   position-fetch failure. `inspectRiskConfig` flags loose/missing template
   guards with severity — a good operator-facing advisor.

7. **Leader monitoring** — dual-redundant: global activity WS (<100ms,
   client-side address substring pre-filter) + per-address on-chain WS
   (~2-3s). **No time cursor, no historical backfill** — WS downtime
   permanently loses trades. 3-layer dedup.

8. **Trade dedup** — Caffeine LRU(txHash) → per-`(leaderId,tradeId)`
   Mutex → DB `processed_trade` UNIQUE. `status=SUCCESS|FAILED` on the
   dedup row. Cleanup deletes rows >10min — a short dedup *window*, not
   durable history (re-copy possible after a >10min gap + restart).

9. **WebSocket** — genuinely WS-driven. Shared client with
   exponential backoff (3s→60s), re-subscribe on reconnect, 10s ping,
   reconnect only on abnormal close. A 30s no-message liveness watchdog
   exists but is commented out.

10. **Multi-account** — `wallet_accounts(proxy_address UNIQUE, ...)`.
    Only MAGIC(sig 1) + SAFE(sig 2); no EOA(0)/POLY_1271(3); `null→SAFE`
    fail-open. AES-encrypted keys. Per-config hard `account_id` FK.
    3-step setup wizard (proxy deploy / API-key derive / USDC approve).

11. **Redeem** — poll-based, trusts the data-api `redeemable` flag.
    NegRisk → WrappedCollateral + a separate WCOL unwrap poller. SAFE
    redeems batched via MultiSend. **Idempotency in-memory only** (lost
    on restart); no redeem-tx-level retry; no confirmation wait in-path.
    429 quota-cooldown circuit-breaker.

12. **DB schema** — dedicated `backtest_task` (14 denormalised result
    columns, indexed `idx_leader_profit`) + `backtest_trade` per-trade
    ledger with `balance_after` equity curve. `leader_research_*` tables.
    `leader_paper_session/trade/position` paper-trading simulator.
    Append-only `leader_activity_event` with dual-uniqueness dedup.

13. **PnL/stats** — strict **FIFO** cost basis (`SellMatchDetail` lots),
    realised = Σ lot PnL, unrealised = qty×(livePrice−buyPrice). Graded
    mark-to-market quote status AVAILABLE/NO_MATCH/UNAVAILABLE. **No fee
    accounting at all.** BigDecimal scale-8.

14. **Strategy config** — config decoupled from templates (template =
    fill-once preset). ~25 knobs incl. tri-mode keyword filter
    (DISABLED/WHITELIST/BLACKLIST), `maxMarketEndDate` upper bound,
    per-market+outcome accumulating position cap.

15. **Observability** — SLF4J string logs, ~200-code banded `ErrorCode`
    enum with i18n keys. 11-endpoint parallel health probe. Per-source
    health persisted. Telegram-only alerts. 429 backoff + quota cooldown.
    Geo-restriction only passively logged.

16. **OpenSpec/demo/docs** — formal SHALL/MUST specs. Key specs:
    `copy-trading-risk-seatbelt` (3-state quote availability, low-confidence
    flag), `leader-research-agent` (separate paper ledger, explainable
    versioned score, money-safety state machine). Docs:
    `DATA_API_CURSOR_PAGINATION` (`start=max(ts)` no `+1`, dedup by txhash),
    `neg-risk-redeem` (dual-contract), `ternary-orderbook` (per-tokenId
    book via `indexSet=2^outcomeIndex`).

## Actionable gaps — tiered

### Tier A — quick, concrete, production correctness

- **A1** Pass `use_server_time=True` to `ClobClient(...)` in
  `shared/polymarket/client.py` — L2 HMAC currently uses container local
  time; a drifting Docker host clock can silently produce signature-
  timestamp rejections.
- **A2** Replace offset pagination in the backtest's `_fetch_leader_trades`
  with the data-api `start` timestamp cursor (`start=max(ts)`, no `+1`,
  dedup by `transactionHash`). Offset silently truncates at the 10k cap
  today (logs a warning only).
- **A3** Ensure a failed copy attempt still persists a `leader_trades`
  idempotency row (PolyHermes' `status=FAILED` slot), so a WS/poll
  redelivery of a known-bad trade is not re-attempted in a loop.
- **A4** Add a relayer 429 quota-cooldown circuit-breaker to the redeem
  path: parse "resets in N s", set a cutoff, short-circuit new redeem
  submissions until it expires.

### Tier B — medium effort, real value

- **B1** Add a `backtest_trades` per-trade ledger table (each fill +
  `equity_after` + `pnl`) and promote ~6 result columns
  (`profit_rate`, `win_rate`, `max_drawdown`, `total_trades`, ...) onto
  `backtest_jobs` so the `/backtest` list can sort/filter without
  unpacking the `result` JSONB.
- **B2** Introduce a 3-state quote availability model
  (AVAILABLE / UNAVAILABLE / NO_MATCH) so an API failure is never counted
  as a confirmed `$0`. Add a low-confidence flag tied to min sample size,
  and a discovery score-version string.
- **B3** Add a `TemplateConfig` risk linter: before an operator enables a
  live follow, warn on null/loose caps with a suggested value (PolyHermes'
  `inspectRiskConfig`).
- **B4** Replace whitelist-only `category_whitelist` with a tri-mode
  keyword filter (disabled / whitelist / blacklist over an operator term
  list) and add an upper market-end-date bound.
- **B5** Give the backtest engine FIFO lot-level cost basis (PolyHermes'
  `SellMatchDetail`) — the current whole-position close keyed on
  `condition_id` mis-prices layered partial entries/exits.
- **B6** Batch SAFE redeems via MultiSend (many resolved markets in one
  tx) to cut gas + relayer-quota usage.

### Tier C — tractable infrastructure

- **C4** Add a persisted market-metadata cache: a `markets` table +
  cache→DB→remote layering, with an immutable settled-price tier. Today
  `LiveDataSource` hits the data-api live every cycle with no persistence.
- **C5** Detect the CLOB geo-restriction error string
  ("Trading restricted in your region") and route it to an explicit
  emergency-stop, instead of only logging it (unhandled on both sides
  today).

## Roadmap items (NOT bug fixes — architectural, design-cycle needed)

- **C1 — forward paper-trading stage.** PolyHermes' single strongest
  idea: a `CANDIDATE→PAPER→TRIAL_READY→COOLDOWN→RETIRED` ladder that
  shadow-copies a leader for ≥7 days / ≥10 simulated trades, score-gated,
  with automatic cooldown-on-degradation, BEFORE any real capital. PolyEcho
  scores discovery purely backward-looking from leaderboard stats — a high
  `final_score` is an unvalidated hypothesis. Adding a paper-trading
  subsystem (paper session/trade/position tables + state machine + score
  integration) is a Phase-5/6-sized initiative, not a fix. Design intent
  captured here for the roadmap.

- **C2 — live WebSocket listener implementation.** PolyEcho's
  `UserChannelListener` / `ChainCrossCheckListener` live loops are
  `raise NotImplementedError` by design (Phase-4 W2); the trade loop is
  poll-based. PolyHermes proves the real-time WS path is viable (<100ms).
  Implementing it drives real-money copy-trades and is a money-path
  change that warrants its own design + test cycle. When built, port
  PolyHermes' reconnect-with-backoff + re-subscribe-on-reconnect + ping
  keepalive, and ENABLE the no-message liveness watchdog (PolyHermes left
  it commented out).

## Correction to one swarm finding

The multi-account agent reported "PolyEcho has no `accounts` table". This
is incorrect — `accounts` exists in `infra/migrations/0000_init.sql`, has a
SQLAlchemy model, an `importAccount` write path, and a 9-case test. The
agent inspected a stale path. No action needed; noted to avoid a future
re-investigation.
