/**
 * Shared, cross-page zh-TW strings + the sidebar.
 *
 * PROPER-NOUN POLICY (applies to every locale module): technical / brand
 * terms stay in English and are NOT translated — Polymarket, CLOB, Data API,
 * Polygon, RPC, VPN, VAPID, PnL, USD, UTC, CLI, API, HTTP, Leader(s),
 * wallet, proxy, datasource, backtest, slippage, latency. Translate the
 * surrounding UI prose only; keep these tokens verbatim inside the values.
 */
export const common = {
  unknown: "未知",
  never: "從未",
  noData: "尚無資料",
  loading: "載入中…",
  none: "—",
  /** Liveness vocabulary reused by sidebar / status / dashboard. */
  status: {
    up: "正常",
    down: "中斷",
    stale: "逾時",
    ok: "正常",
    running: "執行中",
  },
} as const;

export const sidebar = {
  nav: "導覽",
  items: {
    dashboard: "總覽",
    leaders: "Leaders",
    discovery: "探索",
    backtest: "回測",
    trades: "交易",
    positions: "持倉",
    accounts: "帳戶",
    alerts: "通知",
    status: "系統狀態",
    settings: "設定",
  },
  footer: "v0.3 · 僅限 127.0.0.1",
} as const;
