/** Dashboard (`app/page.tsx`) zh-TW strings. Filled by the i18n swarm.
 *  Proper nouns stay English — see common.ts PROPER-NOUN POLICY. */
export const dashboard = {
  title: "總覽",
  subtitle:
    "複製交易概覽。唯讀；暫停與跟單為各自頁面上的明確操作。",
  summaryAriaLabel: "持倉與風險摘要",
  todayPnl: {
    label: "今日淨 PnL",
    caption: "自 00:00 UTC 起開倉的交易。",
  },
  realized: {
    label: "已實現",
    caption: "{{count}} 筆已平倉",
  },
  unrealized: {
    label: "未實現",
    caption: "{{count}} 筆持倉中",
  },
  openRisk: {
    label: "未平倉風險",
    caption: "在場的進場名目金額",
  },
  noTrades:
    "尚無複製交易紀錄。跟隨一位 Leader 後，trader-core 的成交將填入此處。",
  activity: {
    heading: "近期動態",
    latest: "最新 {{count}} 筆",
    empty:
      "目前無動態。trader-core 的成交、錯誤與風險限制通知將顯示於此。",
  },
  coverage: {
    heading: "涵蓋範圍",
    leadersFollowed: "已跟隨的 Leaders",
    openPositions: "未平倉持倉",
    notFollowing:
      "尚未跟隨任何人。在探索頁評估候選對象，再從 Leaders 頁跟單。",
  },
} as const;
