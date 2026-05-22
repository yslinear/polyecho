# PolyHermes C1 + C2 — Implementation Roadmap

> The two architectural roadmap items deferred from
> [`polyhermes-deep-dive.md`](polyhermes-deep-dive.md). Researched by a
> 4-agent swarm (2026-05-22, PolyEcho seams + PolyHermes reference).
> Approved for full implementation. C1 is built first (it is the
> validation layer; lower risk, pure simulation), then C2.

## C1 — Forward Paper-Trading Ladder

A leader must survive a forward shadow-copy run before any real-money
follow. Six-state ladder, auto-promoted by score/days/trade gates. It
NEVER auto-flips to real money.

### Data model (3-way: 0000_init.sql + SQLAlchemy + Drizzle)

- `candidate_leaders.lifecycle_state` — new enum column
  `discovered/candidate/paper/trial_ready/cooldown/retired`, default
  `discovered`. Kept SEPARATE from `user_status` (operator review
  intent: new/watching/following/rejected — unchanged).
- `candidate_leaders` += `cooldown_until TIMESTAMPTZ`,
  `cooldown_count INT DEFAULT 0`, `locked BOOL DEFAULT false` (operator
  override; a locked leader is skipped by the ladder entirely).
- New `paper_sessions` — leader_address, state, started_at, capital_usd,
  score, trade_count, copyable_pnl, max_drawdown, unknown_exposure_ratio,
  filtered_ratio, cursor (last simulated leader-trade ts).
- New `paper_trades` — per-fill ledger (session_id FK, side, condition_id,
  shares, price, fee, pnl, balance_after, ts). Mirrors the B1
  `backtest_trades` shape.
- New `paper_positions` — open FIFO lots per session (survives restart).

### Engine reuse

Extract `BacktestEngine`'s per-trade loop body into
`simulate_one(trade, state)` — the slippage / fee / FIFO (B5) / PnL math
is reused verbatim. Difference: backtest is batch over a fixed window;
paper-trading is incremental with state in `paper_positions` /
`paper_sessions` DB rows, not local dicts.

### New worker — `packages/paper/`

A new uv workspace member + docker service, mirroring
`discovery/__main__.py`'s scheduled loop. Default OFF
(`POLYECHO_PAPER_TRADING_ENABLED=false`). Each ~15-min cycle: for each
`paper`-state leader, fetch new leader trades since the session cursor,
`simulate_one` each, persist, recompute the session summary, re-score
(with the <10-trade score cap at 59), then run the state machine.

NOTE lesson #26: a new workspace member means every
`infra/docker/Dockerfile.*` that runs `uv sync --frozen` must COPY
`packages/paper`.

### State machine + exact gates (ported from PolyHermes)

- `discovered -> candidate`: source seen <=48h AND score >= 60
- `candidate -> paper`: same gate + open a paper_session
- `paper -> trial_ready`: session age >= 7d AND trade_count >= 10 AND
  copyable_pnl > 0 AND max_drawdown >= -15% AND unknown-exposure ratio
  <= 0.20 AND filtered_ratio < 0.50
- `* -> cooldown`: max_drawdown < -20% OR (trade_count >= 10 AND
  copyable_pnl < -5) OR source stale > 72h OR unknown-exposure > 0.50.
  Sets cooldown_until = now + 3d, increments cooldown_count.
- `cooldown -> candidate`: cooldown_until elapsed AND source fresh <=48h
- `cooldown -> retired`: cooldown_count >= 3 OR source not seen > 30d.
  Terminal.
- `locked` leaders are skipped entirely.

### Money-safety boundary (non-negotiable)

`trial_ready` is advisory ONLY. The ladder NEVER inserts a
`follow_relations` row and NEVER sets `enabled=true`. The only path from
`trial_ready` to a real follow is an explicit operator action (a
web/CLI "promote to live follow") gated by the existing danger-passphrase
confirm (`dangerous-action.tsx`). A guard rejects any automated path to
real-money state.

### Phases

- C1-P1 — schema (4 changes) + SQLAlchemy + Drizzle models
- C1-P2 — extract `simulate_one(trade, state)` from `BacktestEngine`
- C1-P3 — `packages/paper/` worker + the state machine + the gates
- C1-P4 — scoring tie-in (the 59-cap below the 60 gate)
- C1-P5 — UI: a `/research` page (lifecycle state + paper stats + the
  operator promote-to-live action)
- C1-P6 — docker service + wiring + the default-off flag

## C2 — Live WebSocket Listeners

Replace poll-based leader-trade detection with real-time WS (<100ms).
The poll/reconcile path stays as the durable safety net.

### Design decision (settled)

Leader-trade detection uses the PUBLIC activity firehose
`wss://ws-live-data.polymarket.com` — leader trades are public, so NO L2
auth is needed. PolyEcho's `UserChannelListener` stub (which assumed
`CLOB_WSS_USER_URL` + auth) is repurposed to consume this firehose. The
authenticated user-channel WS (own-fill confirmation) is a separate,
OPTIONAL phase, deferred — it is not required for leader detection.

### Components

1. A reusable reconnecting async WS client: exponential backoff
   3s -> 60s (x2, reset to 3s on a successful open), re-subscribe on
   reconnect, a `"ping"` keepalive every 10s, reconnect only on an
   abnormal close (code != 1000), AND a 30s no-message liveness watchdog
   (PolyHermes wrote this but left it commented out; PolyEcho ENABLES
   it). Proxy-routed via `get_proxy_url()` (gluetun).
2. `UserChannelListener.run()` -> consumes the activity firehose:
   subscribe `trades` + `orders_matched`, a substring pre-filter by
   leader address before JSON parse, normalize each leader trade into a
   `DetectedTrade`, `queue.put(...)`.
3. `ChainCrossCheckListener.run()` -> a SINGLE multiplexed Polygon
   `eth_subscribe("logs")` connection (NOT one WS per address — that is
   PolyHermes' explicit antipattern), filter CTF/USDC logs by the leader
   set, reconstruct trades, apply the existing N-confirmation guard,
   enqueue.
4. Wiring: in `TraderCore.run`'s TaskGroup, start `ws.run()` +
   `chain.run()` as long-lived siblings plus a queue-drain consumer
   task. Construct the listeners with `live=(datasource == "live")`.
5. Dedup: no change needed. `TradeDeduplicator` keys on
   `(leader_address, tx_hash)` source-agnostically; the WS path and the
   reconcile path dedupe against each other; `leader_trades` UNIQUE is
   the final backstop.

### Safety

WS is only a detection path. Every actual copy still passes the 16-step
risk gate + per-op confirm. WS makes detection faster, it bypasses no
safety. The reconcile loop remains the durable backfill for WS gaps.

### Phases

- C2-P1 — the reusable reconnecting WS client + tests
- C2-P2 — `UserChannelListener.run()`: activity firehose + filter +
  `DetectedTrade` normalization
- C2-P3 — `ChainCrossCheckListener.run()`: multiplexed on-chain logs +
  N-confirmation
- C2-P4 — TaskGroup wiring + the queue-drain consumer + `live=`
- C2-P5 — (optional, deferred) authenticated user-channel for own-fill
  confirmation; needs an L2-cred seam

## Build sequence

Foundational pieces first, then the layers that build on them, CI-green
between waves (the established PolyHermes-fix cadence):

- Wave 1 (foundational, file-disjoint, parallel): C1-P1 schema,
  C1-P2 `simulate_one` extraction, C2-P1 WS client.
- Wave 2 (builds on Wave 1): C1-P3 worker + state machine, C1-P4
  scoring, C2-P2 activity listener, C2-P3 on-chain listener.
- Wave 3 (integration): C1-P5 UI, C1-P6 docker/wiring, C2-P4 TaskGroup
  wiring.
