/** Status group (`app/status/page.tsx`, `components/status-badge.tsx`,
 *  `lib/status.tsx`) zh-TW strings. Filled by the i18n swarm.
 *  Proper nouns stay English — see common.ts PROPER-NOUN POLICY. */
export const status = {
  /** Page header. */
  title: "系統狀態",
  intro:
    "背景 worker 與對外 integration 的唯讀運作狀態。狀態列由 worker 寫入；儀表板不會從這裡啟動、停止或探測任何項目。",
  rowError:
    "無法從資料庫讀取狀態列。以下數據為無法取得，未必代表服務中斷。請檢查 database 探測與主機。",

  /** Workers section. */
  workers: {
    heading: "Workers",
    /** {{minutes}} = stale threshold in minutes (number, not translated). */
    staleHint:
      "當 worker 的最後 heartbeat 超過 {{minutes}} 分鐘未更新時，即視為逾時。沒有狀態列代表它尚未回報，這與已停止的 worker 不同。",
    /** {{time}} = relative time string (already formatted). */
    lastHeartbeat: "最後 heartbeat {{time}}",
    noHeartbeat: "尚無 heartbeat 記錄",
    phase: "階段",
    datasource: "Datasource",
    unknown: "未知",
    /** Heartbeat counter labels. */
    counters: {
      detected: "偵測",
      copied: "複製",
      skipped: "略過",
      placed: "下單",
    },
  },

  /** Integrations section. */
  integrations: {
    heading: "Integrations",
    lastProbeLead: "對外相依項目的最後一次自我探測。",
    /** {{relative}} = relative time, {{absolute}} = absolute UTC timestamp. */
    checkedAt: "已於 {{relative}}（{{absolute}} UTC）檢查。",
    noProbeYet: "尚未執行任何探測。",
    emptyState:
      "尚無 integration 探測資料。當 worker 首次執行連線自我檢查後，便會顯示於此。",
    noDetail: "未回報任何細節",
    latency: "延遲",
    checked: "檢查時間",
    never: "從未",
    /** {{ms}} = latency in milliseconds (number, not translated). */
    latencyMs: "{{ms}} ms",
  },

  /** Worker-liveness verdict labels (lib/status.tsx workerPresentation). */
  verdict: {
    running: "執行中",
    /** {{minutes}} = stale threshold in minutes (number, not translated). */
    stale: "逾時（> {{minutes}} 分鐘）",
    noData: "尚無資料",
    ok: "正常",
    down: "中斷",
  },

  /** Worker heartbeat-key → display name (lib/status.tsx workerLabel). */
  workerLabel: {
    trader: "Trader",
    discovery: "Discovery",
    backtest: "Backtest",
    push: "Push 通知器",
  },

  /** Integration probe-key → display name (lib/status.tsx probeLabel).
   *  Proper nouns (Polymarket, CLOB, Data API, Polygon, RPC, VPN) verbatim. */
  probeLabel: {
    config: "設定",
    database: "資料庫",
    polymarketClob: "Polymarket CLOB",
    polymarketDataApi: "Polymarket Data API",
    polygonRpc: "Polygon RPC",
    vpnEgress: "VPN 對外連線",
  },
} as const;
