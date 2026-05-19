/** Discovery group (`app/discovery/page.tsx`, `app/discovery/[wallet]/
 *  page.tsx`) zh-TW strings. Filled by the i18n swarm.
 *  Proper nouns stay English — see common.ts PROPER-NOUN POLICY. */
export const discovery = {
  /** /discovery — candidate leader pool list page. */
  list: {
    title: "探索",
    subtitle:
      "依總分排序的智慧資金候選者。開啟任一項可查看其歷史，或在跟單前先排入一筆 backtest。",
    newBacktest: "新增 Backtest",
  },
  /** /discovery/[wallet] — leader detail page. */
  detail: {
    notRecorded: "未記錄",
    notSet: "未設定",
    noDataAvailable: "尚無可用資料。",
    back: "返回探索頁",
    runBacktest: "執行 Backtest",
    /** ScoreBadge label props passed from the page file. */
    score: {
      profit: "獲利",
      replicability: "可複製性",
      total: "總分",
    },
    tabs: {
      overview: "概覽",
      stats: "統計",
      history: "分數歷史",
    },
    profile: {
      title: "基本資料",
      sources: "來源",
      strategy: "策略",
      firstDiscovered: "首次發現",
      lastActive: "最近活躍",
      notes: "備註",
    },
    specialization: {
      title: "專精領域",
      none: "尚無專精領域記錄。",
    },
    riskFlags: {
      title: "風險標記",
      none: "無風險標記。",
    },
    breakdown: {
      title: "分數拆解",
      profitHeading: "profit_score（0–100）",
      profitFactors: {
        pnl: "PnL：25%",
        winRate: "勝率：20%",
        sharpe: "Sharpe 比率：20%",
        maxDrawdown: "最大回撤：15%",
        consistency: "穩定度：10%",
        specialization: "專精度：10%",
      },
      replicabilityHeading: "replicability_score（0–100）",
      replicabilityFactors: {
        frequencyMatch: "頻率匹配",
        capitalMatch: "資金匹配",
        burstiness: "爆發性（越低越好）",
        activeHoursOverlap: "活躍時段重疊",
      },
    },
    stats: {
      title: "績效視窗",
      window7d: "7 日",
      window30d: "30 日",
      window90d: "90 日",
      frequencyStrategy: "頻率與策略（§5.3）",
    },
    history: {
      title: "分數歷史",
    },
  },
  /** /discovery DiscoveryTable island — toolbar, columns, row actions,
   *  toasts, empty states, pagination. Proper nouns stay English. */
  table: {
    cardTitle: "候選 Leaders",
    /** Inline placeholder used by null name / strategy / last-active cells. */
    notSet: "未設定",
    /** Search box. */
    searchPlaceholder: "搜尋 wallet 或名稱…",
    searchAria: "依 wallet 或顯示名稱搜尋",
    /** Faceted filter dropdown titles. */
    filters: {
      strategy: "策略",
      status: "狀態",
      riskFlag: "風險標記",
      all: "全部",
      hasRiskFlag: "有風險標記",
      noRiskFlag: "無風險標記",
    },
    /** Status facet option labels (value stays the raw enum). */
    statusOptions: {
      new: "新發現",
      watching: "觀察中",
      following: "跟單中",
      rejected: "已排除",
    },
    /** Column headers (also used by the column-visibility menu). */
    columns: {
      wallet: "Wallet",
      name: "名稱",
      strategy: "策略",
      profit: "獲利",
      replicability: "可複製性",
      total: "總分",
      riskFlags: "風險標記",
      status: "狀態",
      lastActive: "最近活躍",
      actions: "操作",
    },
    /** Cell-level fallbacks. */
    noRiskFlags: "無",
    /** Reset / column-visibility toolbar controls. */
    reset: "重設",
    columnsButton: "欄位",
    toggleColumns: "切換欄位顯示",
    /** Per-row actions menu. */
    actions: {
      label: "操作",
      quickBacktest: "快速 Backtest（90 天 · Balanced）",
      customizeBacktest: "自訂 Backtest…",
      watch: "觀察",
      follow: "跟單",
      reject: "排除",
    },
    /** Sonner toasts fired by row actions. */
    toast: {
      backtestQueued: "Backtest 已排入佇列（90 天 · Balanced）",
      backtestFailed: "Backtest 排入佇列失敗",
      /** {{label}} = the lower-cased action label (translated above). */
      leaderMarked: "Leader 已標記為{{label}}",
      leaderFailed: "更新 Leader 失敗",
    },
    /** Empty states. */
    empty: {
      filteredTitle: "沒有符合篩選條件的 Leader。",
      filteredHint: "清除篩選條件即可看到完整候選名單。",
      clearFilters: "清除篩選",
      noDataTitle: "尚無候選者，請執行 discovery 掃描。",
      noDataHint: "discovery 掃描找到 smart-money wallet 後，名單即會填入。",
    },
    /** Pagination footer. */
    pagination: {
      /** {{first}}–{{last}} of {{total}} (numbers, not translated). */
      range: "{{first}}–{{last}} / 共 {{total}}",
      empty: "0 / 共 0",
      rowsPerPage: "每頁列數",
      rowsPerPageAria: "每頁列數",
      /** {{page}} / {{total}} = page numbers (numbers, not translated). */
      pageOf: "第 {{page}} / {{total}} 頁",
      previousPage: "上一頁",
      nextPage: "下一頁",
    },
    /** {{wallet}} = the wallet address (not translated). */
    rowActionsAria: "{{wallet}} 的操作",
  },
  /** /discovery/[wallet] LeaderScoreChart island. */
  chart: {
    totalScore: "總分",
    noHistory: "尚無分數歷史記錄。",
    trendUp: "上升趨勢",
    trendDown: "下降趨勢",
    trendFlat: "持平",
    /** {{trend}} = a trend* label above. */
    caption: "歷史總分變化，{{trend}}。",
  },
} as const;
