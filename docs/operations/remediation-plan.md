# PolyEcho 修復計畫（11-agent code review backlog）

來源：2026-05-18 11-agent 全專案 code review + 專責資安威脅模型。P0（資安阻斷）已完成並 CI 5/5 green @ `8e93285`。本文件規劃 **P1→P4** 全部剩餘項，供 swarm 依此實作。

## 執行紀律（每一個 wave 皆適用）

- **worktree 隔離**：每個並行 agent 在自己的 git worktree（`isolation: worktree`）作業，套件互斥（disjoint），避免互相覆寫；完成後由 parent 收斂回主樹再驗證。
- **context7 先行**：任何碰外部套件 API 的程式碼，先用 context7 確認官方用法（清單見各項「pkg-verify」）。
- **PolyHermes 參照**：跟單／贖回相關項，以 `docs/reference/polyhermes-analysis.md` §9 + [[polyhermes-phase4-brief]] 為設計依據（非逐字照抄；用 asyncio/httpx/SQLAlchemy 對應）。
- **驗證**：合併樹於乾淨容器 — Python `ruff + mypy strict + POLYECHO_DATASOURCE=fixture pytest（0 failed, cov≥70）+ core/push --once + imports`；web `clean node:22 → pnpm install --frozen-lockfile + lint + test + build`；`docker compose config`。
- **提交/CI**：原子提交一律經 `devkit:git-commit-expert`；`.coverage*`/`.claude/` 絕不入庫；push 後 `gh run watch --exit-status` 對**確切 SHA** 確認 CI 5/5（含 smoke），依 [[ci-verification-discipline]]。**絕不推送已知紅燈**（紅由後續 wave 收綠才一起 push）。
- **錢路安全**：所有下單/贖回維持單一 `assert_live_allowed` 硬鎖，預設 dry-run；P0 的 danger-passphrase / fail-closed 不可回退。
- 每個 wave 結束 checkpoint 到 memory [[polyecho-known-gaps]]。

---

## Wave A — 基礎（先行，序列；其餘 wave 的前置）

### A1 · packages/shared — 3-way schema 對齊 + shared 衛生（凍結後 B 才動）
- **P1-11 schema drift**：對齊 ORM ↔ `infra/migrations/0000_init.sql` ↔ `packages/web/lib/db/schema.ts`：`candidate_leaders.total_score/profit_score/replicability_score` 精度統一 `NUMERIC(6,2)`；`copy_templates.copy_ratio` 精度一致、`exclude_short_markets` 預設一致（SQL/Drizzle=false 為準，修 ORM）；`backtest_jobs.initial_capital_usd/slippage_bps/execution_delay_seconds` nullable 一致；`Event` ORM `index=True` 與 SQL partial index 重複 → 移除 ORM 重複；對齊後**新增「對真 Postgres 套 0000_init.sql 後 reflect 比對 Base.metadata」整合測試**（testcontainers 或 CI service）。
- **P3 shared**：`pg_listen_iter` queue 設 `maxsize`（如 1000，溢位記錄）；`logging._redact_secrets` 收斂 pattern（移除誤殺 `signature_type`，補值層級遮罩）；`config.db_url`/`db_url_drizzle` 改 `SecretStr`。
- pkg-verify：asyncpg LISTEN/NOTIFY、SQLAlchemy 2.0 Numeric/Index、pydantic-settings SecretStr。
- 凍結點：schema 定型，A2 與 B-* 才能依賴。

### A2 · infra + cli(db.py) + ci.yml — 部署自足（依賴 A1 schema 定型）
- **P2 compose 不建 schema**：`infra/docker-compose.yml` 加 one-shot `migrate` 服務（build `Dockerfile.cli`，`command: polyecho db migrate`，`depends_on: db: service_healthy`），其餘服務 `depends_on: migrate: service_completed_successfully`。
- **H2 `db migrate` 壞**：`cli/db.py` 改用真 raw 連線逐敘述執行（asyncpg raw `execute` 或 `exec_driver_sql` + `no_parameters`），多敘述 DDL 不經 prepared path。
- **H3 CI 表數**：`ci.yml` schema/smoke 斷言完整 15 表（`SELECT count(*)` + 全名單）；新增「跑 `polyecho db migrate` against postgres service」步驟（讓文件化路徑被測）；更新 README/0000_init.sql header「12→15」。
- **H4 DB 備份（P2）**：實作 `polyecho db backup`（`pg_dump` → host bind-mount，選配 GCP，加密），README 反映；移除 stub。
- pkg-verify：postgres docker-entrypoint-initdb、docker compose `depends_on` conditions、`pg_dump`。

---

## Wave B — 正確性（A 後並行，worktree 互斥，各自套件）

### B-core · packages/core（跟單引擎，參照 PolyHermes §9/R3/R4）
- **P1-1** 風控 `adjusted_size` 必須傳入 `OrderExecutor`（重建 `Decision`/size override）— live 不可下原始過大量。
- **P1-2** 風控閘 orderbook 取值（`_best/_depth` 的 `x["price"]`）加防護 → 缺欄/畸形 = `DATA_UNAVAILABLE` **fail-closed**（非未處理拋例外）。
- **P1-3** dry-run **不**寫 `FifoLedger`（或獨立 dry-run ledger）→ 不污染 `open_position_count`/portfolio 計數。
- **P1-4** `trader._reconcile_once` 接上真正的 `cursor_writer`（持久化游標）；reconcile 迴圈逐 leader try/except，單 leader 失敗不中止整批、不錯誤推進游標。
- **P1-5** 寫入 `trader_heartbeat`（supervised loop 每輪 + startup）→ `trader status` / Dashboard 可顯示。
- **P1-6** pending `_seen` 改為**成功 + `_finalise` 後**才加入（失敗可重試，不永久放棄）。
- **P1-12** `--once` 退出碼：偵測「全 skip / 0 detect 但有 follows」→ 非零（CI smoke 可偵測壞管線）。
- **P1-13** `TradeDeduplicator._locks` 隨 LRU 連動驅逐（bounded）→ 無記憶體洩漏。
- 跟單正確性對照 polyhermes §9（FAK、makerAddress=proxy、negRisk Exchange、sizing/clamp）— 確認 evaluator/risk 與其一致。
- pkg-verify：py-clob-client-v2（order type/size/tick）、Decimal 精度。

### B-push · packages/push
- **P1-7** NOTIFY/backfill race：先開 `pg_listen` 連線並 `LISTEN`，**再**跑 backfill（gap 事件入 channel buffer）；斷線重連後先跑 mini-backfill 再續 listen。
- **P1-8** `emit_daily_pnl_summary` 加當日日期過濾（`filled_at >= today` settings.tz）。
- **I4** 非 404/410 的 4xx 分類（401/403 → 設定錯誤、停止；413 → content_error 仍 stamp）；**I6** 訂閱 fan-out 加 `asyncio.gather`+semaphore（上限 ~20）避免 head-of-line。
- pkg-verify：pywebpush WebPushException 狀態語意、asyncpg LISTEN buffer 行為。

### B-discovery · packages/discovery
- **P1-9a** `date.today()` → `datetime.now(UTC).date()`（score history 不被本機時區污染）。
- **P1-9b** `_compute_stats` win_rate：`usdc_size is None` 不計入分母（非當虧損）。
- **P1-9c** 高分事件 race：`_upsert_candidate` 的 prev_score 讀取 + threshold + emit 在同一交易內（`SELECT FOR UPDATE` 或 DB flag `high_score_notified_at`）。
- **I1** loop 失敗用短 `retry_sleep`（如 60s，指數退避）而非整個 interval（1d/7d）。
- **I4** `discover_large_new_positions` 分頁或排序截斷修正；**I7** burstiness 註解/實作對齊（CV vs Goh-Barabási，擇一並修正下游 factor）。
- pkg-verify：Polymarket data-api 分頁、SQLAlchemy `with_for_update`。

### B-backtest · packages/backtest（參照 PolyHermes R5；PRD §5.4 必含 slippage+fee+latency）
- **P1-10a fee model**：`BacktestParams` 加 `fee_bps`；fill math 扣 fee（buy/sell），入 trade log + `to_dict()`。
- **P1-10b settlement snap**：`_get_mtm_price` 套用 ≥0.95→$1 / ≤0.05→$0；voided 市場明確處理（回 entry/zero）。
- **P1-10c look-ahead bias**：不在 run 時抓 live `/positions`；改由 trade log 末筆價或 `get_positions(as_of=end_date)` 推導期末價。
- **C4** SELL 無對應 BUY → `skip_reason="no_open_position"`（不灌水勝率）。
- **I5** stale RUNNING→PENDING 復原 sweep（含 cursor resume）；**I6** annualised return 除以 `initial_capital_usd` 非首日 PnL；**I7** activity 分頁改 cursor（或記錄 10k 截斷警告）；**I8** `avg_hold_hours` 用 BUY `opened_at` 非 SELL ts。
- pkg-verify：pandas（PnL curve）、Polymarket data-api cursor 分頁、settlement 規則。

---

## Wave C — 功能補完／硬化（B 後並行，worktree 互斥）

### C-redeem · packages/shared + packages/core（**參照 PolyHermes §9 贖回**；錢路、live-lock 後）
- 實作具體贖回（取代 scaffold）：
  - **redeemable poller**（`CandidateSource`）：poll data-api `redeemable` 旗標（不算 UMA，採其務實 API-trust），gate 於 `auto_redeem + live + 配額冷卻`。
  - **DB upserter**：`redeem_jobs` 冪等 on `(account,condition,outcome)`，狀態機 pending→submitted→confirmed→failed + retry。
  - **call**：`redeemPositions(collateral, 0x0 parentCollectionId, conditionId, [2^outcomeIndex])` on CTF `0x4D97…6045`；**collateral：normal=pUSD `0xC011…2DFB` vs NegRisk=WrappedCollateral `0x3A3B…02E2`，NegRisk 後須 `unwrap(to,amount)` 於 WCOL（輪詢 ~20s）— 最高風險細節**。
  - **per-mode 執行**：EOA → `web3.py` 直送（**本 wave 可具體實作並測試**）；MAGIC → Builder-Relayer PROXY/EIP-191；SAFE → Builder-Relayer SafeTx/EIP-712 或 manual `execTransaction`（SAFE 批次 MultiSend DELEGATECALL；MAGIC 序列 CALL）。**`py-builder-relayer-client` PyPI 不可驗證 → 保持抽象介面 + `# TODO(verify)`，不臆造 import**；EOA 路徑做實。account setup 時 USDC `approve(spender, 2^256-1)` 一次。
  - 全程經 `assert_live_allowed` + `POLYECHO_AUTO_REDEEM`+`POLYECHO_LIVE_TRADING` 雙閘，預設 dry-run。
- pkg-verify：**web3.py**（build/sign/send tx, EIP-712/EIP-191, contract ABI 編碼）、py-clob-client-v2、eth-account/eth-utils；CTF/WCOL/pUSD ABI（`redeemPositions`/`unwrap`/`approve`）。

### C-cli-features · packages/cli（+ 接 discovery/reconcile）
- **C-1/M4** 所有 stub 改 exit≠0（腳本不再靜默成功）。
- 實作 `discovery run/--once/schedule/promote`（接既有 worker `python -m polyecho.discovery` 邏輯）、`logs show`（讀容器/journald/檔）、`db reconcile`（呼叫 C-observability 的鏈上↔DB 對帳）。
- **P3 cli 硬化**：`vapid-keygen` 私鑰行 → stderr（或 `--output-file` 0600）；`resume-all` 在 live 時需 `--yes`/confirm；`db migrate` 加 `--dry-run`+印 redacted DB URL+`--yes`；`doctor` 用 `make_url` 解析（不字串切，避免崩潰洩 URL）；`account import` passphrase 改純 prompt（非可覆寫 Option）。
- pkg-verify：Typer/Click（prompt vs option、exit codes）。

### C-observability · packages/core(+shared) + packages/web
- **鏈上↔DB 對帳模組（P2）**：啟動 + 每日比對「我方持倉/餘額 vs `my_trades`/positions vs 鏈上」（web3.py 讀 ERC1155/USDC），差異記事件/log；供 `db reconcile` 呼叫。
- **效能/metrics（P1.M1/§7.1/§2.3）**：關鍵路徑 `perf_counter` 計時（跟單延遲、discovery、backtest），結構化 log + 可選 Prometheus；§2.3 成功指標 cron 紀錄。
- **heartbeat 上 Dashboard**（web）：讀 `trader_heartbeat` 顯示 bot 存活 + 最後 polling 時間。
- pkg-verify：web3.py（eth_getLogs/balanceOf/ERC1155）、Next.js Server Component data fetch。

### C-web-quality · packages/web（rev-web 殘項）
- `setFollowStatus`/`updateLeaderStatus`/`createBacktestJob`：Server Action 加 server 端輸入驗證（status allowlist、日期格式、數值範圍、`.returning()` 存在性檢查）。
- pkg-verify：drizzle-orm（`.returning()`、型別）、zod（server 端）。

---

## Wave D — 供應鏈/Infra 硬化 + P4 評估（C 後）

- Dockerfile base image 以 `@sha256:` digest 釘選（renovate/dependabot 升）；GitHub Actions 由 tag → SHA 釘選；`web` service 加 healthcheck（HTTP 200 `/`）；`.github/dependabot.yml`（npm+uv+actions）+ 關閉/triage 2 個 dependabot 分支 + 記錄 vite-8 拒絕理由（ignore block）；`.claude/` 加入 `.gitignore`；DB 密碼改非 trivial（或文件化 localhost-only 取捨）；CI 加 Trivy/grype image 掃描；移除 `pnpm install ... || pnpm install` 靜默非 frozen fallback。
- **P4 評估**：嘗試 vite6/vitest3 升級（worktree 試做）；若破 vitest2 → 記錄、維持 deferred（不硬上）。POLY_1271 CREATE2、live WS/Polygon/relayer 維持 `# TODO(verify)`（本質要真錢/真連線才驗，runbook §4 已誠實標示）。
- pkg-verify：vitest 3 / vite 6 breaking changes、Trivy。

---

## 相依與順序

```
A1(shared schema 凍結) → A2(infra/ci/db migrate, 依 A1)
A 完 → B-core ‖ B-push ‖ B-discovery ‖ B-backtest  (4 worktree 並行)
B 完 → C-redeem ‖ C-cli ‖ C-observability ‖ C-web-quality (並行；C-cli `db reconcile` 依 C-observability 對帳模組 → C-observability 先交付對帳，再 C-cli 接)
C 完 → D(供應鏈/infra/P4)
```
每個 wave 邊界：收斂→整合驗證→git-commit-expert 原子提交→push→CI 5/5→memory。錢路（B-core/C-redeem）額外要求保留並驗證 live-lock 不變式與 P0 fail-closed。
