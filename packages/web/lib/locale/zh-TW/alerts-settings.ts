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
      heading: "危險操作",
      help: "手動賣出 / 緊急清倉需在此就地完成二次確認：輸入確認字句並通過危險操作通行碼，方可送出（PRD §5.7）。trader-core 只執行已確認的操作。",
    },
    recent: {
      heading: "最近事件",
      help: "最新在前，最多 {{limit}} 筆。可依嚴重度篩選。",
    },
    /** <AlertsList> client-side severity filter + events table. */
    list: {
      filterGroupAria: "依嚴重度篩選警示",
      filterAll: "全部",
      emptyAll: "目前沒有任何警示。系統發生重要事件時會出現在這裡。",
      emptyFiltered: "此嚴重度沒有符合的警示。",
      colSeverity: "嚴重度",
      colEvent: "事件",
      colSummary: "摘要",
      colPush: "推播",
      colTime: "時間",
      noSummary: "未記錄",
      delivered: "已送達",
      notPushed: "未推播",
    },
    /** <DangerousActionInline> §5.7 危險-tier inline single-step confirm. */
    danger: {
      errNoTarget: "請輸入目標（市場 / 部位 / 帳戶識別）。",
      errNoSize: "手動賣出需要指定數量。",
      openButton: "執行危險操作",
      formAria: "執行危險操作",
      formIntro:
        "這是危險操作（PRD §5.7）。請於下方就地完成二次確認：輸入確認字句並通過危險操作通行碼。通過後即直接送出，trader-core 只執行已確認的操作。",
      typeLabel: "操作類型",
      typeManualSell: "手動賣出",
      typeEmergencyLiquidate: "緊急清倉",
      targetLabel: "目標",
      targetPlaceholder: "市場 / 部位 / 帳戶 ID",
      sizeLabel: "賣出數量",
      sizePlaceholder: "例如 100",
      cancel: "取消",
      /** Inline repeated-confirmation section. */
      confirmSectionTitle: "二次確認（不可復原）",
      /** {{phrase}} = the verbatim confirm phrase (literal, not translated). */
      confirmPhraseLabel: "請完整輸入「{{phrase}}」",
      /** {{phrase}} = the verbatim confirm phrase (literal, not translated). */
      errConfirmPhrase: "請完整輸入「{{phrase}}」以確認此危險操作。",
      errNoPassphrase: "請輸入危險操作通行碼。",
      errConfirmFailed: "確認失敗，請檢查資料庫連線。",
      passphraseLabel: "危險操作通行碼",
      passphrasePlaceholder: "輸入通行碼",
      confirmAria: "確認並執行此危險操作",
      confirming: "送出中…",
      confirmSubmit: "確認並執行",
    },
  },
  settings: {
    title: "設定",
    intro:
      "全域交易設定。所有變更皆透過刻意設計的 server action 寫入；危險操作仍保留在 CLI。",
    proxy: {
      heading: "Polymarket proxy",
      help: "每一筆 Polymarket 請求的對外 HTTP proxy。留空則直接連線。",
      /** <ProxyForm> client island. SOCKS / http(s) / URL kept verbatim. */
      errSocks: "不支援 SOCKS，請改用 http(s)。",
      errScheme: "Proxy URL 必須以 http:// 或 https:// 開頭。",
      errInvalidUrl: "請輸入有效的 http(s) URL，例如 http://127.0.0.1:8080",
      errSaveFailed: "無法儲存 proxy，請檢查資料庫連線。",
      urlLabel: "Proxy URL",
      urlPlaceholder: "http://127.0.0.1:8080  (留空 = 直接連線)",
      formHelp:
        "在 Polymarket 受地區封鎖時需要設定（PRD §8.1），僅支援 http(s)。下一次 Polymarket 請求即生效，無需重啟。",
      saving: "儲存中…",
      save: "儲存 proxy",
      currentDirect: "目前：直接連線",
      /** {{at}} = formatted timestamp (already formatted). UTC kept verbatim. */
      lastSaved: "上次儲存 {{at}} UTC",
      notSaved: "尚未儲存",
    },
    riskMode: {
      heading: "旅行 / 保守模式",
      help: "保守模式會自動收緊跟單比例與風險上限（PRD §5.6），適合外出或無法即時盯盤時使用。trader-core 即時讀取，立即生效，無需重啟。",
      /** <RiskModeForm> client island segmented control. */
      groupAria: "風險模式",
      normal: "正常模式",
      conservative: "保守 / 旅行模式",
      /** {{mode}} = resolved mode label (normal / conservative). */
      current: "目前：{{mode}}",
      errUpdateFailed: "無法更新風險模式，請檢查資料庫連線。",
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
