# PolyHermes → PolyEcho Phase-4 Design Brief

> Source: read-only study of [`WrBug/PolyHermes`](https://github.com/WrBug/PolyHermes)
> (Kotlin/Spring Boot, 180★, **42 Flyway migrations**), the PRD §11.3
> multi-account reference, by a 5-agent research swarm (2026-05-18).
> This is a **design input for Phase 4 (live copy-trading)**, not code to port.
> Their stack is JVM/Spring/MySQL; ours is Python 3.12 asyncio / SQLAlchemy 2.0 /
> py-clob-client-v2 / Postgres. Adopt the *patterns*, not the implementation.

## 1. Architecture (R1)

Layered Spring monolith: `api/` (Retrofit clients) → `service/copytrading/{monitor,orders,statistics,configs,research}` → JPA. Centralised `PolymarketConstants` (all CLOB/Gamma/data-api/WSS URLs in one place). OpenSpec spec-driven, with explicit **live-money safety rules** (conservative defaults, operator confirmation, audit trail, distinguish "zero" vs "unavailable" position).

**PolyEcho:** centralise endpoints in `shared/constants.py`. Keep `docs/PRD.md` §5.5 authoritative the way their OpenSpec is. Use asyncio TaskGroups + APScheduler, not Spring `@Scheduled`/event-bus/Retrofit-bean DI.

## 2. Polymarket integration (R2)

Trading is **CLOB-direct** (not via relayer; relayer is only for on-chain redeem/unwrap). L1 (EIP-712) → derive/create API key → L2 (HMAC) client. Orders are **EIP-712 V2**, `makerAddress = proxyAddress`, negRisk selects a different Exchange contract. Production order type is **FAK** (Fill-And-Kill ≈ IOC). Market data: Gamma (+ cache→DB→Gamma layering); orderbook from CLOB.

**PolyEcho — decisive:** **do NOT hand-roll EIP-712/HMAC/rounding** — that is PolyHermes' single biggest risk, taken only because the JVM had no SDK. We have `py-clob-client-v2`: use its built-in signing/auth (`create_or_derive_api_creds`, `use_server_time=True`, `retry_on_error=True`). Map their `signatureType` → `ClobClient(signature_type=…)`, `proxyAddress` → `funder=`. Fetch per-market **negRisk AND tickSize** from Gamma and pass to `create_order(options=…)` — fixes their static-tick gap. Default FAK/FOK for taker, expose GTC for maker. Surface/halt on geo-restriction (they only log it).

## 3. Copy-trade engine (R3) — the Phase-4 core

**Detection = dual-redundant** (not polling): (A) primary Activity-WS, low-latency <100ms; (B) fallback per-address on-chain RPC logs (~2-3s), reconstructing the trade from USDC/ERC-1155 **token-flow deltas** (not CTF ABI events). Plus a third path watching *their own* proxy wallets for manual/redeem exits.

**Idempotency = 3 layers:** in-proc LRU(txHash) + per-`(leader,tradeId)` lock + persistent `processed_trade(leader_id, tx_hash)` UNIQUE. **txHash is the cross-source dedup key.**

**Pipeline:** dedup → BUY/SELL branch → compute size → filter chain → best-ask sanity → risk gate → optional delay → sign → **FAK submit** → `copy_order_tracking` (notification_sent=false) → reconciliation job backfills real fill price/qty.

**Sizing:** `copy_ratio` `DECIMAL(20,8)`; RATIO=leaderSize×ratio, FIXED=amount/price; clamp ≥1 share, round USDC ↑ to minOrderSize / ↓ to maxOrderSize; buy=leaderPrice+tol% (≤0.99), reject < best-ask. **Sell = FIFO** over unmatched buys keyed `(config, conditionId, outcomeIndex)`; store `leader_buy_quantity` for proportional exits.

**PolyEcho:** prefer the **authenticated WS User Channel** (structured per-user events — strictly better than their global-stream substring scan; no client-side filtering) as path A; Polygon CTF log subscription as path B cross-check. One `asyncio.Queue` → `evaluate_copy` (PRD §5.5). Dedup via `leader_trades(leader_id, tx_hash)` UNIQUE + an in-proc lock/LRU. Borrow the token-flow reconstruction for path B. FAK/FOK + a reconciliation task. Single multiplexed subscription (NOT 1-WS-per-address — won't scale). Retry ≤2, re-sign fresh salt.

## 4. Risk / config / multi-account (R4)

Templates are **copy-from presets, decoupled from the live per-(account,leader) config** (their V4). Pre-trade gate order (**cheap→expensive, ONE orchestrated orderbook fetch reused, fail-closed**: a position/orderbook fetch failure ⇒ **reject**): dedup → creds → conditionId → size → filters(keyword→marketEndDate→price-band→spread→depth→positionValue) → size-normalize(adjust, skip only if infeasible) → daily caps(orders, loss) → slippage-vs-best-ask → sign. Accounts keyed by **proxyAddress UNIQUE** (same signer → N accounts), wallet-type enum. Failures = a `FAILED` status on the dedup row + opt-in async notify (no heavy failed-trade table).

**Conservative preset ceilings (use for PRD §5.6 Conservative):** maxDailyOrders ≤20, maxDailyLoss ≤$10, minPrice 0.10, maxPrice 0.80, maxPositionValue ≤$10, minOrderDepth ≥100, maxSpread ≤0.03, priceTolerance ≤3%; server-side whitelist + explicit `confirm:true`.

**PolyEcho — snapshot `copy_templates` into `follow_relations` on apply** (not FK-bound). Implement the exact layered fail-closed gate in `trader-core`. `accounts` unique key = proxy/funder, add wallet-type.

## 5. Data model / jobs / reconcile / notify (R5)

FIFO realised PnL via `copy_order_tracking` + `sell_match_record` + `sell_match_detail`. Append-only `leader_activity_event` with `stable_event_key` (txhash) = single ingestion source. Schedulers: research 900s, order-status sync 5s, processed-trade cleanup 600s, backtest worker 10s with **restart recovery (stale RUNNING→PENDING, resume from cursor)**. Backtest = cursor-paginated Data-API replay + settlement snap ($1 if ≥0.95 / $0 if ≤0.05). Realtime push = in-memory WS session map.

**PolyEcho:** adopt FIFO tracking tables + the append-only event log idea + backtest restart-recovery cursor. Use **Postgres LISTEN/NOTIFY + `web-push-sender`** (their in-memory map is single-instance / lost on restart). Backtest engine **must add slippage + fee + latency** (their model has none → over-optimistic; our PRD §5.4 already specifies these).

## 6. Wallet-mode gap (user-flagged, confirmed)

PolyHermes has only **2** effective wallet types: `MAGIC` → signatureType **1** (POLY_PROXY), `SAFE` → signatureType **2** (POLY_GNOSIS_SAFE). EOA (0) defined-but-unused. **It has no `POLY_1271` (signatureType 3 / Deposit Wallet) at all.** PolyEcho supports **4** (PRD §5.1; `shared/accounts.py` `SignatureType` 0/1/2/3, `POLY_1271=3` verified via `py-clob-client-v2` `SignatureTypeV2`). ⇒ **There is NO PolyHermes reference for the type-3 path**; rely on `py-clob-client-v2(signature_type=3)` + our `accounts.py`, and the standing rule: the proxy/safe factory **CREATE2 constants are undocumented → live trading MUST use an explicit operator-supplied/confirmed funder, never trust address derivation** for types 1/2/3.

## 7. Where PolyEcho must IMPROVE on PolyHermes (safety)

PolyHermes is a strong functional reference but has accepted-risk gaps a money-handling tool should not inherit:

1. **No reorg/confirmation wait** — acts on first WS log. PolyEcho: add an N-confirmation guard / reconcile-correction on the cross-check path.
2. **`maxDailyLoss` is realized-only** — open-position drawdown escapes. PolyEcho §5.5: add a mark-to-market / open-loss circuit breaker.
3. **No cross-leader/cross-account portfolio cap** — only per-(account,leader). PolyEcho: add a portfolio-exposure layer.
4. **They removed the position-count cap (V26)** — PolyEcho: KEEP a concurrency/position-count cap (concentration risk).
5. **No historical backfill on WS gap** — downtime loses trades. PolyEcho §7.3/§7.4: persist a per-leader `last_seen_ts` cursor; reconcile from the Data API on scheduler tick and after restart.
6. **Telegram-only failure visibility** — PolyEcho: a queryable skip/error log with a skip-reason taxonomy.
7. **Backtest has no slippage/fee/latency** — PolyEcho §5.4 keeps its slippage+latency model (already designed).

## 8. Explicit "do NOT copy"

Hand-rolled EIP-712/HMAC/parseUnits/rounding (use the SDK); hardcoded exchange addresses / static tick-size; JPA + dual serializers + Retrofit-bean DI + Spring event bus + MySQL/BIGINT-epoch columns + `@Scheduled` annotations (use asyncio/APScheduler/SQLAlchemy/Postgres types); in-memory-only WS push; one WS connection per address; `bestBid×0.9` crude sell pricing (size against book depth); silent geo-restriction handling.

## 9. Per-wallet-mode order logic & Redeem (deep dive R6 — Phase-4 binding)

**Order signing is mode-AGNOSTIC.** Wallet mode only sets two things: the order's `signatureType` int and the `maker`/`funder` address. The EIP-712 hash/domain/exchange/rounding/salt are identical across modes. `py-clob-client-v2` does ALL of this natively — **do not reimplement signing/rounding/v-packing** (PolyHermes' own comments cite v-normalization & verifyingContract bugs as the top hazard). Phase-4 needs only a per-account resolver `wallet_mode → (signature_type, funder)`:

| Mode | signature_type | funder (maker) |
|---|---|---|
| EOA | 0 | signer EOA itself |
| MAGIC / email | 1 (POLY_PROXY) | Magic proxy = CREATE2: `keccak256(0xff ++ 0xaB45c5A4B0c941a2F231C04C3f49182e1A254052 ++ keccak256(eoaAddr) ++ 0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b)[12:]` |
| SAFE / web3 | 2 (POLY_GNOSIS_SAFE) | Safe proxy via RPC `computeProxyAddress(eoa)` on Safe factory `0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b` |
| **POLY_1271** | **3** | **explicit operator-supplied funder** — NO PolyHermes reference; our differentiator |

These CREATE2 constants partially resolve the earlier "factory undocumented" caveat for the Magic case, BUT the standing rule holds: **prefer an explicit operator-confirmed funder; treat derivation as a convenience to verify, never to trust blindly** (esp. type-3). NegRisk exchange is per-market (orthogonal to wallet mode) — pass through to py-clob-client-v2. Fail-closed on unknown wallet mode (PolyHermes silently defaults null→SAFE — we must NOT).

**Redeem / claim-winnings (PolyEcho needs this; Phase-4 W1 contract):**
- Trigger: poll Polymarket data-api `redeemable` flag (do NOT compute UMA/on-chain resolution — adopt their pragmatic API-trust). Gate on `auto_redeem_enabled` + relayer/key configured + quota-cooldown.
- Call: `redeemPositions(collateral, 0x0(parentCollectionId), conditionId, [2^outcomeIndex...])` on **CTF `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`**. **Collateral = pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` for normal markets vs WrappedCollateral `0x3A3BD7bb9528E159577F7C2e685CC81A765002E2` for NegRisk** — highest-risk detail to get right. NegRisk redeem pays WCOL → must then `unwrap(to,amount)` on WCOL (PolyHermes polls 20s).
- Execution per mode: MAGIC → Builder-Relayer PROXY (EIP-191 personal_sign); SAFE → Builder-Relayer SafeTx (EIP-712) or manual `execTransaction` if no builder key; EOA → direct `web3.py` tx. **Use the official `py-builder-relayer-client` (PRD §11.2) for gasless; `web3.py` for EOA/manual — do NOT hand-roll the relayer/HMAC.** SAFE batches via MultiSend DELEGATECALL; MAGIC = sequential CALLs (no MultiSend).
- **Improve on PolyHermes:** they use in-memory dedup only. PolyEcho W1 adds a durable `redeem_jobs` table — `(id, account_id, condition_id, outcome_index, index_set, is_neg_risk, status[pending|submitted|confirmed|failed], tx_hash, detected_at, confirmed_at)`, idempotent on `(account_id, condition_id, outcome_index)` (cf. `backtest_jobs`; survives restart, no double-redeem). USDC `approve(spender, 2^256-1)` once at account setup.
- Relayer auth: HMAC builder creds — official client owns it; don't reimplement the interceptor.

Confidence: HIGH for MAGIC/SAFE (full source). EOA(0)/POLY_1271(3): **confirmed absent in PolyHermes** — implement independently via py-clob-client-v2 native `signature_type=0/3` + explicit funder, no blueprint to follow.
