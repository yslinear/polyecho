# PolyEcho 上線（Live Trading）操作手冊

> 對象：本系統的維運者（你本人）。
> 目的：把 PolyEcho 從預設的 **dry-run（不動真錢）** 安全切換到 **live（真實下單／贖回）**，並在切換前充分認知尚未對真實 Polymarket 驗證過的路徑風險。
>
> 前提認知：本系統設計上只綁 `127.0.0.1`、無 auth；遠端存取由你自行在 repo 外處理，屬專案範圍外（PRD §5.9）。握私鑰的 `trader-core` 不開任何 port。

---

## 0. 一句話風險聲明

預設狀態下，每一筆 CLOB 下單與鏈上贖回都會在單一鎖點 `polyecho.shared.copytrading.assert_live_allowed` 直接 `raise LiveTradingLockedError`，只記錄「打算做什麼」而不執行。**只有當你在主機把環境變數設成 `POLYECHO_LIVE_TRADING=1` 時，這個鎖才會打開。** Web 介面沒有、也不會有任何解鎖路徑。

在打開鎖之前，請務必讀完第 4 節「尚未驗證的 live-only 路徑」。

---

## 1. 鎖的模型（必懂）

| 開關 | 位置 | 作用 |
|---|---|---|
| `POLYECHO_LIVE_TRADING` | 主機環境變數（預設未設＝關） | **主鎖**。關＝所有下單／贖回在 `assert_live_allowed` 拋錯，只記 intent。開＝放行真實執行。 |
| `POLYECHO_AUTO_REDEEM` | 主機環境變數（預設未設＝關） | 自動領獎／贖回。**只有當這個與 `POLYECHO_LIVE_TRADING` 同時為真才作用**。 |
| `settings.live_trading_ack` | DB（由 CLI `enable-live` 寫入） | **僅為稽核紀錄**，不是綁定開關。寫了它也不會解鎖；真正的開關只有上面的主機環境變數。 |
| `risk_mode`（settings row） | DB（web /settings 或 CLI 切換） | `normal` / `conservative`。`conservative` 會把 copy ratio 與各風控上限收緊（PRD §5.6）。 |

鎖點是唯一的：`packages/core` 的下單與贖回都經過 `OrderExecutor` →（含確認後的危險操作執行器、redeem worker）→ `assert_live_allowed`。沒有任何旁路。

---

## 2. 上線前 Pre-flight 檢查清單（逐項打勾，缺一不可）

1. **帳號金鑰已匯入且加密**：以 CLI 匯入私鑰（passphrase 衍生 AES-256-GCM），passphrase 你記得、未寫進 `.env`。
2. **錢包模式確認**：清楚該帳號 `wallet_mode` 屬於哪一種，並理解其 `signature_type` / `funder`：
   - `EOA` → sig 0，funder = 自身位址
   - `MAGIC` → sig 1，funder = CREATE2 衍生 proxy
   - `SAFE` → sig 2，funder = RPC `computeProxyAddress` 衍生
   - `POLY_1271`（type-3 Deposit Wallet）→ sig 3，**無 PolyHermes 參照、factory CREATE2 常數未公開文件化 → 必須由你顯式提供 funder 位址**
3. **funder 位址人工對帳**：上鏈用 explorer 確認系統推導出的 funder/proxy 位址，**就是實際持有 USDC、你打算動用的那個位址**。對 SAFE / MAGIC / 特別是 POLY_1271，衍生並非 byte-exact 保證，這一步是強制的，不可略過。
4. **VAPID 與推播就緒**（你要在外面收得到警報）：
   - `polyecho push vapid-keygen` → 把輸出的三行 `POLYECHO_VAPID_*` 貼進主機 `.env`（私鑰是機密，勿提交、勿外流；重產金鑰會讓既有訂閱全部失效需重訂）
   - 在 iPhone Safari 用「加入主畫面」把 Dashboard 裝成 PWA（iOS 16.4+），開啟通知權限並完成訂閱
   - 確認 `push` 服務在跑、能收到測試推播
5. **延長 dry-run 觀察**：在鎖仍關閉下讓系統實際追單一段時間，確認 `/trades`、`/positions` FIFO 已配對/剩餘、`/alerts` 警示中心、風控閘行為、leader 暫停/恢復都正常。
6. **最小曝險起步**：第一次 live 必用 `risk_mode=conservative` ＋ 極小 copy_ratio ＋ 低 `maxPositionValue` / `maxDailyLoss` / `maxDailyOrders`。把第一場當成「用最小金額驗證 live 路徑」，不是賺錢。
7. **第 4 節的 live-only 路徑你已逐項讀過並接受風險**。
8. **kill switch 已備妥**（見第 6 節），手機與主機都能立即觸發。

---

## 3. 切換到 Live 的步驟

> 全程在主機操作；Web 無法解鎖。

1. **稽核儀式（可選但建議）**：`polyecho trader enable-live`
   風險面板 → 5 秒倒數 → passphrase 解密驗證 → 寫入 `settings.live_trading_ack` 稽核列。
   再次提醒：這只是稽核紀錄，**不會解鎖**。
2. **設定主鎖**：在主機 `.env`（或 service 環境）設定：
   ```
   POLYECHO_LIVE_TRADING=1
   # 僅在你也要自動贖回時才加下一行
   # POLYECHO_AUTO_REDEEM=1
   ```
3. **重啟 `trader-core`**（讓新環境生效）：透過 `docker compose` 重啟對應服務；確認啟動 log 出現 `SYSTEM_BOT_RESTART` 事件並收到推播。
4. **冒煙確認鎖已開**：觀察首筆符合條件的跟單 — log 應不再是 `LIVE_LOCKED` / dry-run intent，而是實際送單；到 `/trades` 與 `/alerts` 對照。
5. **緊盯第一場**：守著第一筆真實成交與其 FIFO 出場，確認金額、方向、價格、餘額變化都符合預期再離開。

---

## 4. 尚未對真實 Polymarket 驗證過的 live-only 路徑（務必先讀）

以下程式路徑在程式碼中以 `# TODO(verify)` 標記，**只有 fixture replay 被測試過，從未對真實 Polymarket／鏈上跑過**。鎖打開後它們才會第一次真的執行：

1. **live WebSocket User-Channel 訂閱** 與 **live Polygon log／token-flow 重建**（偵測 leader 真實成交的兩條冗餘路徑）。fixture replay 已驗證邏輯，但真實連線、認證、訊息格式、reorg 行為未實測。
   → **上線前手動驗證**：拿一筆已知的 leader 真實交易，確認系統的 detection 能在合理延遲內正確重建出該筆（方向／市場／數量／價格），再信任自動跟單。
2. **gasless 贖回（`py-builder-relayer-client`）**：該套件在 PyPI 無法驗證，目前是抽象介面 + `# TODO(verify)`，**具體 gasless relayer 贖回並未實作**。
   → 若需自動贖回：先別開 `POLYECHO_AUTO_REDEEM`；改以手動／CLI 流程處理贖回，待該介面被實作並驗證後再啟用。
3. **POLY_1271 / type-3 錢包**：CREATE2 factory 常數無公開文件，衍生非 byte-exact。
   → 對此模式**必須顯式提供 funder 位址並人工對帳**（見 §2.3），不要依賴自動衍生。

結論：第一次 live 請以「最小金額 + 人工盯場 + 已手動驗證 detection」三者同時成立為前提。

---

## 5. 上線後監控

- **Web Push 警示中心** `/alerts`：歷史事件、嚴重度過濾、待確認危險操作面板。
- **推播事件**會在以下情況發送：跟單成交、平倉/停損/達標、每日 PnL 摘要（23:00 台灣時間）、日虧損/策略漂移風控觸發、系統異常（bot 重啟、API 失敗、餘額不足）、候選池高分 leader、待確認危險操作。
- **CLI**：`polyecho trader status`（含 heartbeat）、`polyecho alerts list --severity warning`、`polyecho alerts list --unpushed`。
- **結構化 JSON log**：每個容器的 log 都應乾淨；`assert_live_allowed` 放行後不應再有 `LIVE_LOCKED`。
- **每日對帳**：DB 紀錄 vs 鏈上實際持倉/餘額。

---

## 6. Kill switch / 緊急回退

由快到慢、由輕到重：

1. **全域暫停（最快，Web 或 CLI）**：Dashboard 頂部一鍵全域暫停，或 `polyecho trader pause-all`。停止新跟單，不動既有持倉。
2. **保守模式**：把 `risk_mode` 切 `conservative`，立即收緊 copy ratio 與所有上限。
3. **關閉主鎖（回到絕對安全）**：
   - `polyecho trader disable-live`（清稽核狀態）
   - 主機 `.env` 移除 / 設 `POLYECHO_LIVE_TRADING`（與 `POLYECHO_AUTO_REDEEM`）
   - 重啟 `trader-core`；之後所有下單/贖回重新被 `assert_live_allowed` 擋下，回到 dry-run。
4. **緊急清倉**：屬 PRD §5.7 危險操作層 — 經 Web 觸發 → 5 秒倒數 → passphrase 二次確認 → `confirmed` 後由 core 執行（仍走 `assert_live_allowed`，故鎖關著時不會真的送單；要清倉時鎖必須是開的）。

---

## 7. 一頁速查

```
狀態查詢      polyecho trader status
看警報        polyecho alerts list [--severity warning|critical] [--unpushed]
產 VAPID 金鑰  polyecho push vapid-keygen        # 貼進主機 .env，私鑰保密
稽核儀式      polyecho trader enable-live        # 僅稽核，不解鎖
真正解鎖      主機 .env: POLYECHO_LIVE_TRADING=1 → 重啟 trader-core
自動贖回      主機 .env: POLYECHO_AUTO_REDEEM=1  # 且需先驗證 relayer 路徑（§4.2）
緊急暫停      polyecho trader pause-all          # 或 Dashboard 全域暫停
回到安全      polyecho trader disable-live + 移除 POLYECHO_LIVE_TRADING + 重啟
```

> 黃金守則：**先最小金額、先人工驗證 detection、先盯場，再規模化。** 鎖是預設關著的，這是刻意設計；打開它是你主動承擔真實資金風險的明確動作。
