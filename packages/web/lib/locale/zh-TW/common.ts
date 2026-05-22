/**
 * Shared, cross-page zh-TW strings + the sidebar.
 *
 * PROPER-NOUN POLICY (applies to every locale module): technical / brand
 * terms stay in English and are NOT translated — Polymarket, CLOB, Data API,
 * Polygon, RPC, VPN, VAPID, PnL, USD, UTC, CLI, API, HTTP, Leader(s),
 * wallet, proxy, datasource, backtest, slippage, latency, heartbeat.
 * Translate the surrounding UI prose only; keep these tokens verbatim
 * inside the values. (`heartbeat` is an ops term — PRD + status.ts use it
 * in English throughout; do not render it as 心跳.)
 *
 * TERMINOLOGY LOCKS:
 *  - copy-trade → 跟單 everywhere (never 複製交易 / 複製 for the concept;
 *    複製 stays only for literal clipboard-copy, e.g. tables.copyAddress).
 *  - position is an INTENTIONAL domain split, kept on purpose:
 *      持倉 = live holdings (trades / positions / dashboard),
 *      部位 = backtest sizing (backtest module).
 *    These are distinct concepts; do NOT unify them.
 *  - zh-TW noun/verb: 紀錄 = the noun ("a record"), 記錄 = the verb
 *    ("to record"). Use the correct form for the sentence.
 */
export const common = {
  /** Generic fallback for an unmapped enum value (used by lib/format.ts
   *  signatureTypeLabel). 未知 = "unknown". */
  unknown: "未知",
  /** Visually-hidden chrome a11y copy we own (root-layout skip-link +
   *  the persistent sidebar-toggle in the top header). shadcn/ui primitive
   *  sr-only text stays upstream-verbatim. */
  a11y: {
    skipToContent: "跳至主要內容",
    toggleSidebar: "切換導覽側欄",
  },
  /** Generic chart island empty-state (shared DataChart wrapper). */
  chart: {
    noData: "尚無分數歷史記錄。",
  },
  /** <ScoreBadge> tier labels (text, never colour-only). */
  scoreBadge: {
    high: "高",
    mid: "中",
    low: "低",
    notSet: "未設定",
  },
  /** <DataTable> toolbar / empty-state / pagination prose. */
  dataTable: {
    search: "搜尋…",
    columns: "欄位",
    all: "全部",
    /** Honest default empty state (DESIGN.md "Tables: real empty state"). */
    empty: "目前尚無資料可顯示。請調整上方篩選條件，或在資料匯入後再回來查看。",
    /** {{count}} = filtered row count (number, not translated). */
    rowCount: "{{count}} 列",
    /** {{page}} / {{total}} = page numbers (numbers, not translated). */
    pageOf: "第 {{page}} / {{total}} 頁",
    previousPage: "上一頁",
    nextPage: "下一頁",
  },
  /** <PushSubscribe> Web Push opt-in island. */
  push: {
    enable: "開啟推播通知",
    disable: "關閉推播",
    working: "處理中…",
    enableAria: "開啟此裝置的推播通知",
    disableAria: "關閉此裝置的推播通知",
    subscribed: "此裝置已訂閱警示推播。",
    notSubscribed: "尚未在此裝置開啟推播。",
    unsupported:
      "此瀏覽器不支援 Web Push。請改用支援的瀏覽器，iPhone 需先將本站以 Safari『加入主畫面』安裝為 App（iOS 16.4 以上）。",
    iosHint:
      "iPhone 使用者：先用 Safari 開啟本站，點分享後選『加入主畫面』，從主畫面圖示開啟 App 再開啟推播（iOS 16.4 以上才支援）。",
    unknownDevice: "未知裝置",
    otherOs: "其他",
    otherBrowser: "瀏覽器",
    errNoVapid: "尚未設定 VAPID 公鑰，請先在後端設定推播金鑰。",
    errPermission: "未授予通知權限，無法開啟推播。",
    errSubscribe: "訂閱推播失敗，請確認瀏覽器支援並重試。",
    errUnsubscribe: "取消訂閱失敗，請重試。",
    okUnsubscribe: "已關閉此裝置的推播通知。",
  },
  /** <LiveTradingStatus> read-only live-money lock indicator (PRD §5.7). */
  liveTrading: {
    locked: "LOCKED",
    unlocked: "UNLOCKED",
    /** {{state}} = locked/unlocked label (kept English by policy). */
    pillLabel: "Live trading {{state}}",
    /** {{state}} = locked/unlocked label (kept English by policy). */
    pillText: "Live {{state}}",
    titleLocked:
      "Live trading 已 LOCKED。綁定開關是主機環境變數 POLYECHO_LIVE_TRADING（僅限 CLI）。",
    titleUnlocked:
      "Live trading 已在稽核紀錄中確認。綁定開關仍是主機環境變數 POLYECHO_LIVE_TRADING（僅限 CLI）。",
    /** Verbose explainer; POLYECHO_LIVE_TRADING/CLI kept verbatim by policy. */
    explainerBefore: "這是唯讀指示。實際控制 live 資金的綁定開關是主機環境變數 ",
    explainerAfter:
      "，由 trader-core 讀取，且僅能在 CLI 變更（PRD §5.7 high-danger 等級）。儀表板刻意不提供啟用或停用 live trading 的任何控制。",
  },
  /** <TraderHeartbeatStatus> read-only follow-engine liveness (PRD §7.4).
   *  `heartbeat` kept English by PROPER-NOUN POLICY (ops term). */
  heartbeat: {
    /** {{time}} = relative time string (already formatted). */
    running: "跟單引擎：運行中 · 最後 heartbeat {{time}}",
    /** {{minutes}} = stale threshold in minutes (number, not translated). */
    stale: "跟單引擎：停止 / 無 heartbeat（> {{minutes}} 分鐘）",
    missing: "跟單引擎：無 heartbeat",
    titleRunning: "跟單引擎正在運行。此為唯讀指示，無啟停控制。",
    /** {{minutes}} = stale threshold in minutes (number, not translated). */
    titleStale: "跟單引擎無新 heartbeat 超過 {{minutes}} 分鐘。此為唯讀指示，無啟停控制。",
  },
} as const;

export const sidebar = {
  /** Accessible name for the primary <SidebarMenu> nav landmark. */
  navAria: "主要導覽",
  /** Section labels — the flat nav list is clustered into these groups. */
  groups: {
    discovery: "探索與評估",
    trading: "交易與帳戶",
    monitoring: "監控",
  },
  items: {
    dashboard: "總覽",
    leaders: "Leaders",
    discovery: "探索",
    backtest: "回測",
    research: "Paper 階梯",
    trades: "交易",
    positions: "持倉",
    accounts: "帳戶",
    alerts: "通知",
    status: "系統狀態",
    settings: "設定",
  },
  footer: "v0.3 · 僅限 127.0.0.1",
} as const;
