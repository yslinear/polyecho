# PolyEcho — Polymarket 跟單系統 PRD

|項目                 |內容                          |
|-------------------|----------------------------|
|專案名稱（品牌）           |**PolyEcho**                |
|Repo 名稱            |`polyecho`                  |
|CLI 指令             |`polyecho`                  |
|Domain（預留）         |`polyecho.app`              |
|Python distribution|`polyecho`                  |
|Python module      |`polyecho`                  |
|Docker namespace   |`polyecho`                  |
|版本                 |v0.3 (Draft)                |
|日期                 |2026-05-17                  |
|作者                 |Shan                        |
|狀態                 |規劃中                         |
|目標部署               |家裡 macOS / GCE e2-micro (備援)|

-----

## 0. 命名規範

PolyEcho 對外採用「**單字小寫**」的識別字（`polyecho`），與 Polymarket 生態的 `polyterm`、`polyhermes` 一致；對內品牌與文件採用 PascalCase。

|用途                                          |形式              |範例                                                |
|--------------------------------------------|----------------|--------------------------------------------------|
|品牌名、文件、Logo、UI 標題                           |PascalCase      |`PolyEcho`                                        |
|GitHub repo、CLI 指令、Docker image namespace   |單字小寫            |`polyecho`                                        |
|子模組 / 子套件                                   |kebab-case（內部短名）|`core`、`discovery`、`web`                          |
|Python distribution / module                |單字小寫            |`polyecho`、`polyecho.core`                        |
|TypeScript / npm scope                      |單字小寫            |`@polyecho/core`、`@polyecho/web`                  |
|Postgres database 名稱                        |單字小寫            |`polyecho`                                        |
|環境變數前綴                                      |UPPER_SNAKE     |`POLYECHO_DB_URL`                                 |
|**執行中的進程 / Docker container / systemd unit**|kebab-case，功能描述性|`trader-core`、`discovery-worker`、`web-push-sender`|

**為什麼不用 hyphen（`poly-echo`）？**

- Polymarket 自家專有名詞工具都用單字：`polymarket`、`polyterm`、`polyhermes`
- Domain `polyecho.app` 不需要 hyphen，搜尋與輸入體驗最好
- npm/PyPI 上單字 package 比較好記、不需教使用者要不要加 hyphen
- Docker image `polyecho/core` 比 `poly-echo/core` 視覺更乾淨

**命名分層**：套件名（`core`）→ Python 模組（`polyecho.core`）→ 進程名（`trader-core`）→ Docker image（`polyecho/core`）。同一個元件在不同層級用不同命名，各自符合該層級的慣例。

-----

## 1. 背景與動機

### 1.1 Polymarket 現況

Polymarket 為全球最大去中心化預測市場，2026 年 5 月時點：

- 24h 活躍市場橫跨政治、體育、加密、總體經濟、世界事件
- Politics（2028 民主黨候選人）、Sports（2026 FIFA）、Economy（Fed 決議）為三大主流類別
- 2026 年 2 月 18 日移除 500ms taker 延遲，maker/taker 策略生態大幅變動
- 2026 年 4 月推出 10x 永續合約（BTC/NVDA/Gold）
- CFTC 起訴紐約州、巴西封禁，**監管風險持續升高**

### 1.2 為什麼做跟單

直接交易需要對市場有深度了解；跟單策略可以利用 Polymarket 鏈上資料完全公開的特性，借助「聰明錢」的研究成果。

**現有第三方跟單工具的問題：**

- 多數是付費服務（Betmoar、PolyTrack、Whale Tracker Livid）
- 開源版本品質參差不齊，許多 README 充斥 SEO 關鍵字垃圾（PolyScripts 等）
- 部分需要把私鑰交給 Telegram bot 的中心化伺服器，安全性堪憂
- 通用工具無法針對個人資金規模、延遲、風險偏好做最佳化

### 1.3 為什麼自建

1. **私鑰自主**：所有簽名都在本機，私鑰永不外流
1. **演算法可調**：候選池過濾條件、回測參數、風險旗標都可自訂
1. **整合既有 stack**：充分利用已熟悉的 GCP、Supabase、Next.js
1. **長期演化空間**：未來可演進為 SaaS 或私人朋友群使用
1. **法律可控**：完全在自己控制下，符合台灣個人投資定位

-----

## 2. 目標與非目標

### 2.1 Goals (本版本範圍)

- **G1** 自動發現並持續追蹤潛在的優質 Leader 候選池
- **G2** 對任一 Leader 提供帶滑價/延遲模型的 PnL 回測
- **G3** 對已選定的 Leader 進行自動跟單，含完整風控
- **G4** 提供 Web 儀表板（綁定 `127.0.0.1`，僅限本機）以監控與簡單操作；遠端存取由維運者自行處理，屬專案範圍外
- **G5** 私鑰處理達到「即使 Web UI 被入侵也無法盜取資金」的安全等級
- **G6** 系統 7×24 運行，當機自動恢復、操作可追蹤

### 2.2 Non-goals (本版本不做)

- ❌ 多租戶 SaaS、對外公開服務
- ❌ 為他人代管資金或私鑰
- ❌ 高頻交易、做市、套利策略
- ❌ 主動追蹤內幕交易者（合規風險）
- ❌ 法幣出入金（純 USDC / pUSD 操作）
- ❌ 衍生品（永續合約）跟單
- ❌ Mobile App（用 Web 響應式設計即可）

### 2.3 成功指標

|指標                      |目標值                    |量測方式                          |
|------------------------|-----------------------|------------------------------|
|Discovery worker 每日運行成功率|≥ 99%                  |Cron job 紀錄                   |
|候選池規模                   |≥ 50 名活躍 leader        |DB query                      |
|跟單延遲（leader 下單 → 你下單）   |< 5 秒（中位數）             |比對鏈上時間戳                       |
|Replicability 模型誤差      |實盤 PnL 與回測 PnL 偏差 < 30%|月度回顧                          |
|系統可用性                   |> 99%                  |本機健檢 + bot heartbeat        |
|安全事件                    |0 起私鑰外洩                |持續監控                          |

-----

## 3. 使用者與情境

### 3.1 主要使用者

**Shan（單一使用者）**

- 技術背景：IT 基礎建設、Python、Next.js、Supabase、GCP
- 設備：Mac (家)、iPhone (外)、台北網路
- 風險偏好：保守，重視可解釋性與安全性
- 投入規模：初期 $500–$2,000 USDC 試水溫

### 3.2 主要使用情境

**情境 A：發現新 Leader（每週幾次，在家）**

1. 打開 Dashboard，看候選池本週新增/分數變動
1. 對前 3–5 名跑 90 天回測（不同滑價情境）
1. 通過評估的加入 watching status，觀察 1–2 週
1. 確認後正式啟用跟單

**情境 B：日常監控（每天，在家或在外）**

1. 在家直接 `localhost` 開 Dashboard；在外則經由維運者自備的遠端存取方式（VPN / tunnel 等，專案範圍外）連線
1. 看當日 PnL、開倉、leader 動態
1. 偶爾調整 copy ratio 或暫停某個 leader

**情境 C：緊急介入（隨時，任何地方）**

1. 收到 Web Push 推播（虧損達門檻、leader 策略漂移、API 異常）
1. 點推播 → iPhone Safari 直接開啟 Dashboard（在外經由維運者自備的遠端存取方式）
1. 連線後執行：全域暫停 / 暫停特定 leader / 手動賣出
1. 嚴重時 SSH 進去（遠端通道由維運者負責，專案範圍外）做更深的介入

**情境 D：旅行模式（離家 > 7 天）**

1. 出發前在 Dashboard 啟用「保守模式」（降低 copy ratio、收緊風控）
1. 旅行中 Web Push 持續，但儘量不做主動調整
1. 回家後做完整檢討

-----

## 4. 系統架構

### 4.1 高層拓樸

```
┌─────────────────────────────────────────────────────┐
│ 家裡 macOS (主節點，UPS + 自動重啟)                  │
│                                                     │
│  ┌─────────────────┐   ┌──────────────────────┐    │
│  │ trader-core     │   │ discovery-worker     │    │
│  │ (Python, 24/7)  │   │ (Python, cron)       │    │
│  │ 握私鑰、簽單     │   │ 發現+打分+風險       │    │
│  │ 不開任何 port   │   └──────────┬───────────┘    │
│  └────────┬────────┘              │                │
│           │                       │                │
│           ▼                       ▼                │
│  ┌─────────────────────────────────────────┐       │
│  │ PostgreSQL 16                            │       │
│  │ (LISTEN/NOTIFY for instant sync)        │       │
│  └────────┬─────────────────────────────────┘       │
│           │                                         │
│           ▼                                         │
│  ┌─────────────────┐   ┌─────────────────────┐     │
│  │ Next.js 15      │   │ web-push-sender     │     │
│  │ (Dashboard)     │   │ (Python, VAPID)     │     │
│  │ 唯讀為主         │   │ 推播 + 警示中心     │     │
│  └────────┬────────┘   └─────────────────────┘     │
│           │                                         │
│  ┌────────▼──────────────────────────────┐         │
│  │ Dashboard bind 127.0.0.1:3000 (本機)  │         │
│  │ 無 auth（設計如此）                    │         │
│  └────────────────────────────────────────┘         │
└─────────────────────────────────────────────────────┘
              ↑
              │ 遠端存取（VPN / tunnel 等）
              │ 由維運者自備，專案範圍外
              │
       ┌──────┴─────┐
       │  iPhone    │  ←  戶外存取
       └────────────┘
```

### 4.2 進程拆分原則

|進程                       |是否拿私鑰|是否開 port     |通訊方式                         |
|-------------------------|-----|-------------|-----------------------------|
|`trader-core`            |✅    |❌            |只讀寫 Postgres                 |
|`discovery-worker`       |❌    |❌            |只讀寫 Postgres                 |
|`dashboard-web` (Next.js)|❌    |✅ (127.0.0.1) |讀寫 Postgres；綁本機，遠端存取屬範圍外    |
|`web-push-sender`        |❌    |❌            |讀 events 表 + VAPID 推播至訂閱端點   |
|`postgres`               |❌    |✅ (localhost)|TCP/IP                       |

**關鍵設計**：唯一握私鑰的進程不對外開任何網路 port，所有控制透過 DB 設定變更實現。

### 4.3 部署選項

|選項                      |成本   |可靠性       |隱私|
|------------------------|-----|----------|--|
|**A. 家裡 Mac**           |$0   |中（家裡網路、停電）|高 |
|**B. GCE e2-micro**     |~$7/月|高         |中 |
|**C. 混合：Mac 主 + GCE 備援**|~$7/月|高         |高 |

**初期選 A，旅行前升級到 C。**

### 4.4 Monorepo 結構

```
polyecho/                              # GitHub repo
├── packages/
│   ├── core/                          # trader-core：握私鑰、簽單下單
│   │   ├── pyproject.toml
│   │   └── src/polyecho/core/
│   ├── discovery/                     # discovery-worker：候選池發現
│   │   ├── pyproject.toml
│   │   └── src/polyecho/discovery/
│   ├── backtest/                      # 回測引擎
│   │   ├── pyproject.toml
│   │   └── src/polyecho/backtest/
│   ├── push/                          # web-push-sender：VAPID 推播
│   │   ├── pyproject.toml
│   │   └── src/polyecho/push/
│   ├── cli/                           # polyecho 指令工具
│   │   ├── pyproject.toml
│   │   └── src/polyecho/cli/
│   ├── shared/                        # 共用：Polymarket client、models、db
│   │   ├── pyproject.toml
│   │   └── src/polyecho/shared/
│   └── web/                           # Next.js dashboard
│       ├── package.json
│       ├── app/
│       ├── components/
│       └── lib/
├── infra/
│   ├── docker-compose.yml             # 一鍵起 Postgres + 全部 services
│   ├── docker-compose.dev.yml         # 開發環境
│   └── migrations/                    # Drizzle schema 遷移
├── docs/
│   ├── PRD.md
│   ├── ARCHITECTURE.md
│   └── operations/                    # 維運筆記
├── scripts/                           # 雜項腳本
├── pyproject.toml                     # Python workspace 根設定（uv workspace）
├── package.json                       # 前端 workspace（pnpm）
├── pnpm-workspace.yaml
└── README.md
```

**Python namespace 設計（PEP 420）**：所有 Python 套件共用 `polyecho` namespace。使用者寫 `from polyecho.core import TraderCore`、`from polyecho.shared.db import session`，即使這些子模組來自不同的 packages 目錄。

**Workspace 管理工具**：

- Python：uv workspace（最快、現代）
- 前端：pnpm workspaces
- 兩者並存，互不干擾

**套件對外命名**：

|層級                           |識別字                                                                |
|-----------------------------|-------------------------------------------------------------------|
|Python distribution（PyPI 若公開）|`polyecho-core`、`polyecho-discovery`、`polyecho-shared` 等           |
|Python import path           |`polyecho.core`、`polyecho.discovery`                               |
|npm package                  |`@polyecho/web`、`@polyecho/shared`                                 |
|Docker images                |`polyecho/core:latest`、`polyecho/web:latest`、`polyecho/push:latest`|
|Docker compose service 名稱    |`core`、`discovery`、`web`、`push`、`db`                               |

### 4.5 CLI 指令設計

```bash
# 帳號管理
polyecho account detect 0xabc...          # 偵測 4 種錢包類型
polyecho account import 0xabc...          # 安全匯入私鑰
polyecho account list                     # 列出已註冊帳號
polyecho account balance <name>           # 查詢餘額

# 候選池
polyecho discovery run --once             # 立即跑一次發現
polyecho discovery schedule               # 啟動排程模式
polyecho discovery promote 0xabc...       # 手動加入觀察名單

# 回測
polyecho backtest new --leader 0xabc --days 90
polyecho backtest list                    # 看歷史回測結果
polyecho backtest show <id>               # 顯示回測詳情

# 跟單執行
polyecho trader start                     # 啟動 trader-core
polyecho trader status                    # 查看狀態
polyecho trader pause-all                 # 全域暫停
polyecho trader pause <leader_addr>       # 暫停特定 leader

# 資料庫
polyecho db migrate                       # 套用 schema 遷移
polyecho db backup                        # 立即備份
polyecho db reconcile                     # 鏈上 vs DB 對帳

# 系統
polyecho doctor                           # 健檢：Config、DB、Polymarket API
polyecho logs <service>                   # 查看服務 log
```

**短別名**：在 `~/.bashrc` 加 `alias pe='polyecho'` 即可。

-----

## 5. 功能模組

### 5.1 帳號管理（Account Management）

**功能**

- 偵測 EOA 對應的 4 種錢包類型（Type 0/1/2/3）
- 安全匯入私鑰（CLI only，**不走 Web UI**）
- 多帳號管理
- 餘額與授權狀態同步

**4 種簽名類型**

|Type|代號              |適用情境                                    |
|----|----------------|----------------------------------------|
|0   |EOA             |MetaMask 直連、未走 polymarket.com onboarding|
|1   |POLY_PROXY      |用 Email/Google (Magic.link) 註冊          |
|2   |POLY_GNOSIS_SAFE|用 MetaMask 連 polymarket.com 註冊（最常見）     |
|3   |POLY_1271       |Deposit Wallet（新 API 使用者推薦）             |

**偵測流程**

1. 使用者於 Web UI 輸入 EOA 地址（公開資訊）
1. 後端並行計算 4 種衍生地址
1. 對每個地址查詢 Polymarket Data API（持倉、餘額、活動）
1. 依「持倉 > 餘額 > 已部署 > 歷史活動」順序推薦類型
1. 使用者確認後寫入 `accounts` 表（`status='pending_key_import'`）
1. 使用者於 CLI 執行 `polyecho account import <eoa>` 匯入私鑰
1. 私鑰用 passphrase-derived key 加密後存入 DB（`encrypted_private_key`）

### 5.2 Leader 候選池（Discovery）

**4 個發現來源**

|來源                    |頻率  |抓什麼                             |
|----------------------|----|--------------------------------|
|Polymarket Leaderboard|每日  |30d 前 200 名獲利者                  |
|熱門市場前 N 持倉            |每日  |每個 24h volume > $100K 市場的前 10 持倉|
|大額新開倉                 |每小時 |過去 24h 內 size > $5K 的新單對應錢包     |
|已知聰明錢清單               |每週手動|社群識別 + 第三方追蹤工具公開資料              |

**雙維度打分**

```
final_score = profit_score × (replicability_score / 100)
```

- `profit_score` (0–100)：PnL 25% + 勝率 20% + Sharpe 20% + 回撤 15% + 一致性 10% + 專精 10%
- `replicability_score` (0–100)：頻率匹配 × 資金匹配 × 突發性 × 時段重疊

**乘法不是加法**：跟不到就是 0。

### 5.3 頻率與策略分析（Frequency Analysis）

**核心指標**

```yaml
trades_per_day_30d: 5.2
active_days_30d: 22
median_gap_minutes: 240
avg_hold_hours: 18
hold_to_resolution_pct: 35
concurrent_positions_avg: 3
concurrent_positions_max: 8
burstiness: 0.4
active_hours_utc: [13, 14, ..., 22]
```

**策略分類**

|類型             |每日交易   |平均持倉    |跟單可行性    |
|---------------|-------|--------|---------|
|Scalper        |>20    |<1h     |❌        |
|Day Trader     |5–20   |1–8h    |⚠️        |
|**Swing**      |**1–5**|**1–7d**|✅ **甜蜜點**|
|Position       |<1     |>1 週    |✅        |
|Hold-to-Resolve|不定     |到結算     |✅        |

**策略漂移偵測**：當 30d 與 7d 策略類型不一致，自動觸發 Web Push 警報 + 暫停跟單。

### 5.4 回測引擎（Backtest Engine）

**輸入**

```python
BacktestParams(
    leader_address: str,
    start_date: date,
    end_date: date,
    initial_capital_usd: float,
    copy_ratio: float,              # 0.001 = 跟對方 0.1%
    max_position_usd: float,
    min_trade_size_usd: float,
    daily_loss_limit_usd: float,
    slippage_bps: int = 150,        # 預設 1.5%
    execution_delay_seconds: int = 30,
    category_whitelist: list[str] = None,
)
```

**核心模擬邏輯**

1. 抓 leader 該期間所有成交（從 Polymarket Data API + 鏈上）
1. 依時間順序逐筆模擬：
- 風控檢查（每日虧損、最大部位）
- 計算 copy size
- 模擬延遲後的成交價（含滑價）
- 檢查該時點流動性是否足夠
- 執行或記錄跳單原因
1. 期末 mark-to-market 所有未平倉部位

**輸出**

- 累積 PnL 曲線（daily series）
- 統計：總報酬、年化、Sharpe、最大回撤、勝率、平均持倉
- 跳單分析：流動性不足 / 風控觸發 / 低於最小金額
- 與 leader 原始 PnL 對照

**三組情境並排**

- 樂觀：滑價 50bps、延遲 10s
- 中性：滑價 150bps、延遲 30s
- 悲觀：滑價 300bps、延遲 60s

### 5.5 實時跟單執行器（Trader Core）

**監聽機制**

- WebSocket 連 Polymarket User Channel 訂閱所有 active leaders
- 同時用 Polygon RPC 監聽 CTF Exchange 合約事件做交叉驗證
- 偵測到新成交 → 寫入 `leader_trades` 表 → 觸發跟單評估

**跟單決策**

```python
async def evaluate_copy(leader_trade) -> Decision:
    # 1. 檢查 leader 是否仍在 following 狀態
    # 2. 載入該 leader 的 copy_template
    # 3. 計算你的目標 size
    # 4. 多層風控：
    #    - 每日虧損 < daily_loss_limit
    #    - 未平倉部位數 < max_concurrent
    #    - 該市場類別在白名單
    #    - leader 無策略漂移 flag
    # 5. 檢查當前 orderbook 流動性
    # 6. 簽單並下單（POLY_GNOSIS_SAFE 或 POLY_1271）
    # 7. 寫入 trades 表 + 觸發 Web Push 事件
```

**訂單類型**

- 預設 FOK (Fill-Or-Kill) 限價單，避免部分成交造成尾巴
- 緊急退出時用 IOC (Immediate-Or-Cancel)

### 5.6 跟單模板（Copy Templates）

```sql
copy_templates (
  id, name,
  copy_ratio NUMERIC,         -- 0.001 = 0.1%
  fixed_size_usd NUMERIC,     -- 或固定金額（互斥）
  max_position_usd NUMERIC,
  min_trade_size_usd NUMERIC,
  max_concurrent_positions INT,
  daily_loss_limit_usd NUMERIC,
  category_whitelist TEXT[],
  exclude_short_markets BOOLEAN, -- 排除 5min/15min crypto
  use_fok_orders BOOLEAN
)
```

預設提供三套模板：

- **Conservative**：0.05% ratio, $20 max, $50 daily loss, 排除短期
- **Balanced**：0.1% ratio, $50 max, $100 daily loss
- **Aggressive**：0.3% ratio, $200 max, $300 daily loss

### 5.7 Web Dashboard（Next.js）

**頁面結構**

```
/                    Dashboard（總覽：PnL、開倉、警示）
/leaders             已跟單對象（暫停/恢復）
/discovery           候選池（排序、篩選、回測入口）
/discovery/[wallet]  Leader 詳細頁（含回測歷史）
/backtest/new        新建回測任務
/backtest/[id]       回測結果
/trades              交易歷史
/positions           即時持倉
/accounts            帳號管理（偵測 + 啟用狀態）
/settings            全域設定
```

**技術選型**

- Next.js 15 App Router
- Server Components 直接讀 Postgres（透過 Drizzle ORM）
- shadcn/ui + Tailwind
- Recharts 做圖表
- 無 auth（依設計綁定 `127.0.0.1` 僅限本機；遠端存取由維運者自行處理，屬專案範圍外）

**操作分級**

|級別  |操作                 |走哪裡                                  |
|----|-------------------|-------------------------------------|
|唯讀  |看持倉、PnL、歷史、候選池     |Web UI 直接讀                           |
|安全寫入|暫停/恢復、改設定、加減 leader|Web UI Server Action 寫 settings 表    |
|危險操作|手動賣出、緊急清倉          |Web UI 觸發 + 5 秒倒數確認 + Passphrase 二次輸入|
|高危險 |提領、改私鑰             |CLI only                             |

### 5.8 Web Push 警示中心

**設計原則**：使用瀏覽器原生 Web Push API + Service Worker，把推播直接送到 iPhone Safari，**零第三方依賴、零私鑰風險**。

**訂閱流程**

1. 第一次造訪 Dashboard（在家 `localhost`，在外經由維運者自備的遠端存取方式），授權通知權限
1. Service Worker 註冊 PushSubscription，存入 `push_subscriptions` 表
1. 後端任何進程要推播時，呼叫 Web Push 協定（VAPID 認證）發送

**推播事件**

- 新跟單成交
- 平倉 / 停損 / 達標
- 每日 PnL 摘要（晚間台灣時間 23:00）
- 風控觸發（日虧損達門檻、leader 策略漂移）
- 系統異常（API 失敗、bot 重啟、餘額不足）
- 候選池新增高分 leader
- 待確認的危險操作

**控制操作**：Dashboard UI 提供

- 全域暫停 / 恢復按鈕（頂部一鍵）
- 每個 leader 旁的暫停 / 恢復
- 設定頁的所有 template 與風控參數
- 「警示中心」獨立頁面：歷史推播紀錄、確認危險操作

**離線情境（外出無網路）**

- iPhone Safari 即使關閉，Web Push 仍能喚醒通知
- 沒網路時無法觸發遠端操作（Dashboard 僅綁本機，遠端通道為維運者責任，屬專案範圍外）
- 系統會在離線期間累積事件，下次上線時補推

**iOS 限制注意**

- iOS Safari 16.4+ 才支援 Web Push（你 iPhone 應該沒問題）
- 必須先把 Dashboard 加到「主畫面」當作 PWA，才能接收 Web Push
- 推播樣式較陽春，但夠用

### 5.9 遠端存取（專案範圍外）

Dashboard 依設計只綁定 `127.0.0.1`（見 §4.2），預設**無 auth**——這是刻意的安全選擇：唯一握私鑰的進程不開任何 port，Web 介面也不暴露於網路。

私有 / 遠端存取（VPN、reverse tunnel、SSH port-forward 等任一機制）**由維運者在 repo 之外自行建置與維護，屬本專案範圍外**。本 PRD 不規定、不內建、也不背書任何特定工具；專案只保證 Dashboard 綁本機且不對外公開。

-----

## 6. 資料模型

### 6.1 核心表

```sql
-- 帳號（你自己的）
accounts (
  id UUID PK, name, eoa_address UNIQUE,
  funder_address, signature_type INT,
  encrypted_private_key BYTEA NULL,
  status TEXT,  -- pending_key_import | ready | disabled
  created_at, updated_at
)

-- 候選 leader 池
candidate_leaders (
  wallet_address PK, display_name,
  sources TEXT[], first_discovered_at, last_active_at,
  stats_7d JSONB, stats_30d JSONB, stats_90d JSONB,
  frequency_stats JSONB, strategy_type,
  backtest_default JSONB,
  profit_score, replicability_score, total_score,
  risk_flags TEXT[], specialization TEXT[],
  user_status TEXT,  -- new | watching | following | rejected
  user_notes, updated_at
)

-- 候選分數歷史
candidate_score_history (
  wallet_address, date, total_score, rank, stats JSONB,
  PRIMARY KEY (wallet_address, date)
)

-- 跟單模板
copy_templates (
  id PK, name, copy_ratio, fixed_size_usd,
  max_position_usd, min_trade_size_usd,
  max_concurrent_positions, daily_loss_limit_usd,
  category_whitelist TEXT[], exclude_short_markets,
  use_fok_orders
)

-- 跟單關係（leader → account → template）
follow_relations (
  id PK, account_id FK, leader_address, template_id FK,
  status,  -- active | paused
  created_at, paused_at
)

-- Leader 交易歷史快取
leader_trades (
  id BIGSERIAL PK, trade_id UNIQUE,
  leader_address, market_id, condition_id,
  side, size, price, timestamp TIMESTAMPTZ,
  raw JSONB
)

-- 你自己的交易
my_trades (
  id BIGSERIAL PK,
  account_id FK, leader_address, copy_of_trade_id,
  market_id, side, size, price,
  fill_status,  -- pending | filled | rejected | partial
  pnl_usd, opened_at, closed_at
)

-- 設定（單列多 key 模式）
settings (
  key TEXT PK, value JSONB, updated_at
)

-- 回測任務
backtest_jobs (
  id UUID PK, leader_addresses TEXT[], template_params JSONB,
  start_date, end_date, initial_capital_usd,
  slippage_bps, execution_delay_seconds,
  status, progress NUMERIC, result JSONB,
  created_at
)

-- 待確認的危險操作
pending_actions (
  id UUID PK, action_type, params JSONB,
  status,  -- awaiting_confirm | confirmed | expired | rejected
  confirm_passphrase_hash, expires_at, created_at
)

-- Web Push 訂閱端點
push_subscriptions (
  id UUID PK,
  endpoint TEXT UNIQUE,
  p256dh_key TEXT, auth_key TEXT,
  device_label TEXT,  -- 'iPhone Safari' 等
  created_at, last_used_at
)

-- 系統事件（給 Web Push sender 推播用）
events (
  id BIGSERIAL PK, event_type, severity,
  payload JSONB, pushed_at, created_at
)
```

-----

## 7. 非功能性需求

### 7.1 效能

- 跟單延遲（中位數）：< 5 秒
- 候選池每日全量重算：< 30 分鐘
- 單筆回測（90 天，預設參數）：< 60 秒
- Web 頁面首屏渲染：< 1 秒（本機存取）

### 7.2 安全

- 私鑰加密儲存（passphrase-derived AES-256-GCM）
- 私鑰永不出現於 Web 進程記憶體
- Web UI 依設計只綁定 `127.0.0.1`，不對外公開；遠端存取由維運者自行處理，屬專案範圍外
- 所有 API 呼叫使用 HTTPS
- 危險操作必須在 Web UI 二次確認（5 秒倒數 + Passphrase）
- 系統 log 不含私鑰、API secret、訂單簽名

### 7.3 可靠性

- 主節點當機後 5 分鐘內自動重啟所有服務
- 資料庫每日凌晨備份至外接硬碟 + GCP Cloud Storage（加密）
- bot 啟動後自動 reconcile 鏈上實際持倉 vs DB 紀錄
- 所有外部 API 呼叫有 retry + circuit breaker

### 7.4 可觀測性

- 結構化 JSON log（journald 或 file）
- 關鍵事件寫 `events` 表供 Web Push sender 推播
- Dashboard 顯示 bot heartbeat（最後一次 polling 時間）
- 每日對帳：DB 紀錄 vs 鏈上實際狀態

-----

## 8. 風險與緩解

### 8.1 法律與合規

|風險                   |緩解                                         |
|---------------------|-------------------------------------------|
|跟到內幕交易者              |主動偵測 `insider_suspect` 旗標自動拒絕              |
|Polymarket 地域限制（台灣狀況）|確認 polymarket.com TOS 與當地法規，必要時使用 VPN（用戶自負）|
|CFTC 監管擴大            |持續關注，必要時暫停或結束                              |
|個人稅務                 |系統 export CSV 供報稅，**不提供稅務建議**              |

### 8.2 技術風險

|風險                                  |緩解                      |
|------------------------------------|------------------------|
|Polymarket API 變更（如 V1 → V2 cutover）|用官方 SDK，密切關注 changelog  |
|市場流動性不足，跟不到單                        |回測時模擬，實盤時 FOK 失敗即放棄     |
|Leader 策略改變                         |策略漂移偵測 + 自動暫停           |
|私鑰外洩                                |分層隔離 + 加密 + Web 介面綁本機不對外  |
|鏈上 reorg                            |等待 N 個確認後才認可 leader 成交  |

### 8.3 操作風險

|風險                |緩解                                  |
|------------------|------------------------------------|
|家裡電腦掛掉            |UPS + 自動重啟 + GCE 備援                 |
|旅行中無法介入           |Web Push + 維運者自備遠端存取（範圍外）+ 保守模式 + 風控自動觸發|
|資金鎖在 USDC，遇到 depeg|限制單個帳號最大資金量                         |

-----

## 9. 開發階段（Roadmap）

### Phase 0：基礎建設（1 週）✅ 完成（2026-05-17）

- [x] 建立 `polyecho` git repo（本地已 init；GitHub remote / push 待使用者）
- [x] Monorepo scaffolding：`packages/{core,discovery,backtest,push,cli,shared,web}/`
- [x] Python workspace 設定（pyproject.toml + `polyecho` PEP 420 namespace，`docker build` 驗證）
- [x] Next.js 15 + Drizzle ORM 在 `packages/web/`
- [x] Postgres schema + Drizzle migrations（DB 名 `polyecho`，`infra/migrations/0000_init.sql`）
- [x] CI/CD（GitHub Actions：python / web / docker / schema jobs）
- [x] Docker images：`polyecho/core`、`polyecho/web`、`polyecho/push`、`polyecho/cli`
- [x] Web 介面綁定 `127.0.0.1`，不對外公開（遠端存取由維運者負責，屬專案範圍外）
- [x] `polyecho doctor` 健檢指令（Config/DB/Polymarket API 真實檢查）

### Phase 1：Discovery + 評分（2 週）✅ 完成（2026-05-17）

- [x] Polymarket SDK 封裝（`polyecho.shared.datasource`：DataSource Protocol + Live/Fixture + factory；live 端點已對官方文件驗證）
- [x] 4 源 discovery worker（leaderboard / 熱門市場持倉 / 大額新開倉 / 聰明錢 seed）
- [x] 頻率與策略分析（`frequency.py`：stats + 5 類策略分類 + drift 偵測）
- [x] 雙維度打分（`scoring.py`：profit × replicability / 100）
- [x] 風險旗標偵測（`riskflags.py`：insider_suspect 自動 reject 等）
- [x] Web 上的候選池頁面（唯讀）（`/discovery` + `/discovery/[wallet]`）

**里程碑**：累積一週後能看到穩定的候選池，識別出甜蜜點 leader。

> **進度註記（2026-05-17）**：Phase 0–1 由 agent swarm 完成；容器內 `pytest packages/shared packages/discovery` → **95 passed / 0 failed**。資料來源預設 fixture（PRD §8.1 台灣地域限制），live client 已實作並藏在 interface 後。尚未 push（使用者掌控）。

### Phase 2：回測引擎（1–2 週）✅ 完成（2026-05-17）

- [x] 模擬引擎核心（`BacktestEngine.run()`：時序逐筆、風控閘、copy size、mark-to-market）
- [x] 滑價/延遲/流動性模型（slippage_bps + 延遲 adverse 移動 + 流動性 skip，假設皆內嵌註記）
- [x] 三情境並排比較（樂觀 50/10、中性 150/30、悲觀 300/60）
- [x] backtest_jobs queue + worker（`jobs.py` poll pending→running→done/failed；`python -m polyecho.backtest`）
- [x] Web 上的回測頁面（`/backtest/new` shadcn form + Server Action 入列；`/backtest/[id]` 結果＋三情境圖）

**里程碑**：對 5 個甜蜜點 leader 完成深度回測，做出實盤跟單決策。

> **進度註記（2026-05-17）**：Phase 0–2 完成並 push（19 commits，`origin/main`）。容器內 `pytest packages/{shared,discovery,backtest,cli}` → **134 passed / 0 failed**。UI 定案：**只深用 shadcn/ui**（PRD §10），dashboard 已換 shadcn sidebar shell + data-table + chart。回測 live MTM 有 1 個 `TODO(verify)`（fixture 路徑不受影響）。

### Phase 3：帳號管理 + 唯讀 Dashboard（1–2 週）✅ 完成（2026-05-17）

- [x] 4 種錢包類型偵測（`polyecho.shared.accounts`：derive_wallets 4 候選 + 依「持倉≫餘額≫活動」evidence 排序的 detect；factory/CREATE2 常數官方未公開 → `# TODO(verify)`，type-0 精確、偵測以鏈上證據排序故不受影響）
- [x] CLI 私鑰匯入工具（`polyecho account detect/import/list/balance`：隱藏 passphrase＋私鑰輸入、AES-256-GCM、CLI-only、絕不記錄；status pending_key_import→ready）
- [x] Dashboard 主頁、交易歷史、持倉頁面（`/` `/trades` `/positions` `/accounts` 唯讀 shadcn；`encrypted_private_key` 永不查詢）
- [x] Web 介面綁定 `127.0.0.1`，不對外公開（任何遠端存取機制由維運者於 repo 外自備，屬專案範圍外）

> **進度註記（2026-05-17）**：Phase 0–3 完成並 push，GitHub CI 4 job 全綠（run 25990373652 @ 65aa99a）。容器 `uv run pytest` **369 passed / 89.62% cov**；web vitest 132 passed。Phase 3 由並行 swarm（p3-accounts ∥ p3-dashboard）完成。

### Phase 4：實時跟單（2–3 週）

- [ ] WebSocket 監聽 + 鏈上監聽
- [ ] 跟單評估引擎
- [ ] 多層風控
- [ ] 訂單下單 + 對帳

**里程碑**：使用最保守 template，連續 7 天無事故跟單。

### Phase 5：Web Push + 風控收斂（1 週）

- [ ] Web Push 訂閱與 Service Worker
- [ ] VAPID 認證設定
- [ ] 警示中心頁面
- [ ] 危險操作二次確認流程
- [ ] 旅行模式

**總計：8–11 週 weekend project**

-----

## 10. 技術選型

|層級              |選擇                                   |理由                                 |
|----------------|-------------------------------------|-----------------------------------|
|Backend 語言      |Python 3.12 + asyncio                |你熟、生態完整、官方 SDK 一級支援                |
|CLOB Client     |py-clob-client-v2                    |官方、最新 V2、支援所有簽名類型                  |
|DB              |PostgreSQL 16                        |你熟（Supabase 同底）、LISTEN/NOTIFY、JSONB|
|ORM (Python)    |SQLAlchemy 2.0 async                 |成熟、async 支援                        |
|ORM (TypeScript)|Drizzle                              |型別安全、輕量、與 Postgres 完美              |
|Frontend        |Next.js 15 App Router                |你熟、Server Components 簡化資料層         |
|UI Kit          |shadcn/ui + Tailwind                 |客製空間大、社群活躍                         |
|圖表              |Recharts                             |與 React 整合好、夠用                     |
|推播              |Web Push API（Service Worker）         |直接推到 iPhone Safari，零第三方依賴          |
|部署              |Docker Compose                       |簡單、可移植                             |
|監控              |systemd + journalctl + Dashboard 警示中心|不裝額外監控系統                           |
|隔離              |Web 綁定 `127.0.0.1`                   |localhost-bound，不對外公開；遠端存取屬專案範圍外      |
|Secret 管理       |passphrase + 加密 DB column            |個人用足夠                              |
|密碼學             |cryptography (Python)                |AES-256-GCM 標準                     |

-----

## 11. 參考文件

### 11.1 Polymarket 官方

- [Polymarket Docs 主站](https://docs.polymarket.com)
- [API Reference](https://docs.polymarket.com/api-reference/introduction)
- [Authentication](https://docs.polymarket.com/api-reference/authentication)
- [Clients & SDKs](https://docs.polymarket.com/api-reference/clients-sdks)
- [Deposit Wallets 指南](https://docs.polymarket.com/trading/deposit-wallets)
- [Quickstart](https://docs.polymarket.com/trading/quickstart)
- [WebSocket Market Channel](https://docs.polymarket.com/api-reference/wss/market)
- [WebSocket User Channel](https://docs.polymarket.com/api-reference/wss/user)
- [Leaderboard API](https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings)
- [Get User Activity](https://docs.polymarket.com/api-reference/core/get-user-activity)
- [Get Current Positions](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user)
- [Negative Risk Markets](https://docs.polymarket.com/advanced/neg-risk)
- [Gasless Transactions](https://docs.polymarket.com/trading/gasless)
- [Fees](https://docs.polymarket.com/trading/fees)
- [Resolution](https://docs.polymarket.com/concepts/resolution)
- [Geographic Restrictions](https://docs.polymarket.com/api-reference/geoblock)
- [Polymarket TOS](https://polymarket.com/tos)

### 11.2 Polymarket 官方 SDK / GitHub

- [py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2) — Python（主要採用）
- [clob-client-v2](https://github.com/Polymarket/clob-client-v2) — TypeScript
- [rs-clob-client-v2](https://github.com/Polymarket/rs-clob-client-v2) — Rust
- [Polymarket/agents](https://github.com/Polymarket/agents) — 官方 AI Agent 框架
- [py-builder-relayer-client](https://github.com/Polymarket/py-builder-relayer-client) — Relayer Python client
- [poly-market-maker](https://github.com/Polymarket/poly-market-maker) — 官方做市範例
- [Polymarket org 全部 repos](https://github.com/polymarket)

### 11.3 開源實作參考

- [WrBug/PolyHermes](https://github.com/WrBug/PolyHermes) — Kotlin/Spring Boot 跟單平台，多帳號管理參考
- [Drakkar-Software/OctoBot-Prediction-Market](https://github.com/Drakkar-Software/OctoBot-Prediction-Market)
- [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot)
- [NYTEMODEONLY/polyterm](https://github.com/NYTEMODEONLY/polyterm) — CLI 終端，whale/smart money 訊號參考
- [leolopez007/polymarket-trade-tracker](https://github.com/leolopez007/polymarket-trade-tracker) — PnL 分析、鏈上資料解析參考
- [Vadim-Kolchanov/polymarket-client](https://github.com/Vadim-Kolchanov/polymarket-client)

### 11.4 工具與資源清單

- [harish-garg/Awesome-Polymarket-Tools](https://github.com/harish-garg/Awesome-Polymarket-Tools)
- [aarora4/Awesome-Prediction-Market-Tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools)

### 11.5 文章與分析

- [How to build a Polymarket trading bot (after new rules edition)](https://dev.to/xniiinx/how-to-build-a-polymarket-trading-bot-after-new-rules-edition-5h09)
- [Build a Polymarket Trading Bot with Python](https://robottraders.io/blog/polymarket-trading-bot-python)
- [預測市場 Polymarket 完整解析（鏈新聞 ABMedia）](https://abmedia.io/prediction-market-polymarket-tutorial)
- [Polymarket BTC 5-Minute Up/Down Trading Bot (Gist)](https://gist.github.com/Archetapp/7680adabc48f812a561ca79d73cbac69)

### 11.6 技術棧官方文件

- [Next.js Docs](https://nextjs.org/docs)
- [shadcn/ui](https://ui.shadcn.com)
- [Drizzle ORM](https://orm.drizzle.team)
- [Recharts](https://recharts.org)
- [Tailwind CSS](https://tailwindcss.com)
- [PostgreSQL LISTEN/NOTIFY](https://www.postgresql.org/docs/current/sql-listen.html)
- [SQLAlchemy 2.0 Async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [pywebpush](https://github.com/web-push-libs/pywebpush)
- [VAPID (RFC 8292)](https://datatracker.ietf.org/doc/html/rfc8292)
- [MDN: Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API)
- [iOS Safari Web Push Support](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
- [cryptography (Python)](https://cryptography.io/)

### 11.7 法規與監管參考

- [Polymarket Privacy Policy](https://polymarket.com/privacy)
- [Polymarket Status](https://status.polymarket.com)
- [Polymarket Accuracy](https://polymarket.com/accuracy)

-----

## 12. 附錄

### 12.1 名詞表

|術語             |說明                                             |
|---------------|-----------------------------------------------|
|**EOA**        |Externally Owned Account，使用者控制的以太坊帳號           |
|**CLOB**       |Central Limit Order Book，Polymarket 的訂單簿       |
|**CTF**        |Conditional Token Framework，Polymarket 用的條件代幣標準|
|**Funder**     |Polymarket 訂單中持有資金的合約地址                        |
|**Maker/Taker**|提供流動性的人 / 吃流動性的人                               |
|**FOK**        |Fill-Or-Kill，全成或取消的訂單                          |
|**IOC**        |Immediate-Or-Cancel，立即成交剩餘取消                   |
|**Leader**     |被跟單的對象                                         |
|**Copy Ratio** |跟單比例，相對於 leader 的 size                         |
|**Slippage**   |滑價，預期成交價與實際成交價的差距                              |
|**Drawdown**   |從高點到低點的回撤幅度                                    |
|**Burstiness** |交易間隔的群聚程度，0 = 均勻、1 = 高度群聚                      |

### 12.2 未決議題（後續確認）

- [ ] 是否需要支援 Polymarket 永續合約跟單？（目前傾向否）
- [ ] Discovery worker 是否應該主動爬第三方 leaderboard（如 polymarketanalytics.com）？
- [ ] 是否實作「反向跟單」（針對被識別為「frequent loser」的人）？
- [ ] 多大規模後考慮專業託管？

-----

**文件結束**

*本 PRD 是活文件，預計每完成一個 Phase 後修訂一次。*
