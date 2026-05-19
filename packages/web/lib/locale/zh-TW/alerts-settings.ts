/** Alerts + Settings group (`app/alerts/page.tsx`, `app/settings/
 *  page.tsx`) zh-TW strings. Filled by the i18n swarm.
 *  Proper nouns stay English — see common.ts PROPER-NOUN POLICY.
 *  (Polymarket / HTTP / Web Push / VAPID / CLI / trader-core / PRD §
 *  are technical/brand tokens kept verbatim inside the values.) */
export const alertsSettings = {
  alerts: {
    title: "警示中心",
    intro:
      "系統事件、推播狀態與待確認的危險操作。本頁只讀取與確認，所有警示由 trader-core 與推播服務產生。",
    push: {
      heading: "推播通知",
      help: "在此裝置開啟 Web Push，重要事件即時送達。每個裝置各自訂閱。",
    },
    pending: {
      heading: "待確認的危險操作",
      help: "手動賣出 / 緊急清倉需 5 秒倒數與通行碼二次確認（PRD §5.7）。trader-core 只執行已確認的請求。",
      empty: "目前沒有待確認的操作。",
      meta: "建立於 {{created}} · 剩餘 {{remaining}}",
      noParams: "（無參數）",
    },
    recent: {
      heading: "最近事件",
      help: "最新在前，最多 {{limit}} 筆。可依嚴重度篩選。",
    },
  },
  settings: {
    title: "設定",
    intro:
      "全域交易設定。所有變更皆透過刻意設計的 server action 寫入；危險操作仍保留在 CLI。",
    proxy: {
      heading: "Polymarket proxy",
      help: "每一筆 Polymarket 請求的對外 HTTP proxy。留空則直接連線。",
    },
    riskMode: {
      heading: "旅行 / 保守模式",
      help: "保守模式會自動收緊跟單比例與風險上限（PRD §5.6），適合外出或無法即時盯盤時使用。trader-core 即時讀取，立即生效，無需重啟。",
    },
    push: {
      heading: "推播通知",
      helpPrefix: "在此裝置開啟 Web Push，重要事件即時送達。完整警示記錄請見",
      helpLink: "警示中心",
      helpSuffix: "。",
    },
    copyTemplates: {
      heading: "跟單範本與風險",
      help: "保守 / 平衡 / 積極範本、每日虧損上限與類別白名單目前由 CLI 編輯，後續階段會移至此處。",
    },
  },
} as const;
