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
} as const;
