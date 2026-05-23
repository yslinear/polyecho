/** Discovery group (`app/discovery/page.tsx`, `app/discovery/[wallet]/
 *  page.tsx`) zh-TW strings. Filled by the i18n swarm.
 *  Proper nouns stay English — see common.ts PROPER-NOUN POLICY.
 *
 *  Phase-1 merge (Option C, moderate): the leaderboard `/discovery` and the
 *  paper-trading ladder `/research` are now ONE chip-filtered candidates
 *  funnel page. /research permanently redirects in. The user_status filter +
 *  column + actions are dropped from the UI (schema/CLI cleanup is a separate
 *  commit; the keys are removed here so the i18n orphan-guard stays green).
 */
export const discovery = {
  /** /discovery — merged candidate funnel list page. */
  list: {
    /** "候選漏斗" — the page is now the full funnel from leaderboard ranking
     *  through ongoing paper-trading evaluation, trial_ready, retired. */
    title: "候選漏斗",
    subtitle:
      "依分數排序的候選 Leader 漏斗。透過上方分頁切換不同階段：排行榜、驗證中、可升 live、已退休。",
    newBacktest: "新增 Backtest",
    /** Chip strip: 5 state filters above the table. Each chip writes
     *  `?state=…` so the view is shareable / refresh-stable. */
    chips: {
      discovered: "排行榜",
      active: "驗證中",
      trialReady: "可升 live",
      retired: "已退休",
      all: "全部",
      /** Accessible name on the chip strip nav landmark. */
      ariaLabel: "候選漏斗階段",
    },
    /** Trial-ready chip badge — small count pill, only rendered when > 0. */
    trialReadyBadgeAria: "可升 live 候選數量：{{count}}",
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
    /** Header status Badge — the raw candidate_leaders.user_status enum
     *  (new/watching/following/rejected). An UNMAPPED status degrades to
     *  the raw enum verbatim (never a crash). The detail page is OUT OF
     *  SCOPE of the merge cleanup (operator: schema/CLI cleanup is a
     *  separate commit), so these labels remain. */
    statusLabel: {
      new: "新發現",
      watching: "觀察中",
      following: "跟單中",
      rejected: "已排除",
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
      /** Raw `stats_*` / `frequency_stats` JSONB keys produced by the
       *  discovery worker. An UNMAPPED key falls back to the raw key
       *  verbatim (so a new worker key degrades to English, never crashes). */
      fields: {
        pnl_usd: "PnL（USD）",
        win_rate: "勝率",
        sharpe: "Sharpe 比率",
        max_drawdown: "最大回撤",
        consistency: "穩定度",
        specialization: "專精度",
        trades_per_day: "每日交易數",
        total_trades: "總交易數",
        first_trade_at: "首次交易時間",
        active_days: "活躍天數",
        median_gap_minutes: "交易間隔中位數（分鐘）",
        avg_hold_hours: "平均持有時數",
        hold_to_resolution_pct: "持有至結算比例",
        concurrent_positions_avg: "同時持倉數（平均）",
        concurrent_positions_max: "同時持倉數（最高）",
        burstiness: "爆發性（越低越好）",
        active_hours_utc: "活躍時段（UTC）",
      },
    },
    history: {
      title: "分數歷史",
    },
  },
  /** /discovery DiscoveryTable island — toolbar, columns, row actions,
   *  toasts, empty states, pagination. Proper nouns stay English.
   *
   *  Phase-1 merge: chip-aware columns + chip-aware default sort. The
   *  user_status column/filter is dropped (schema unchanged, just hidden
   *  from the UI). Several columns appear only on the chips that need
   *  them (lifecycle_state, copyability, paper stats, promote action). */
  table: {
    cardTitle: "候選 Leaders",
    /** Inline placeholder used by null name / strategy / last-active cells. */
    notSet: "未設定",
    /** Search box. */
    searchPlaceholder: "0x… 或 Leader 名稱",
    searchAria: "依 wallet 或顯示名稱搜尋",
    /** Faceted filter dropdown titles (kept; the strategy + risk-flag
     *  facets still apply to the leaderboard chip + 全部 chip). */
    filters: {
      strategy: "策略",
      riskFlag: "風險標記",
      all: "全部",
      hasRiskFlag: "有風險標記",
      noRiskFlag: "無風險標記",
    },
    /** Range filters for the 0–100 score columns (獲利 / 可複製性 / 總分). */
    range: {
      profit: "獲利區間",
      replicability: "可複製性區間",
      total: "總分區間",
      min: "0",
      max: "100",
      sliderAria: "{{title}}（0–100 雙向拉桿）",
      current: "{{min}} – {{max}}",
      badge: "{{min}}–{{max}}",
      unbounded: "不限",
      clear: "清除",
    },
    /** strategy_type enum DISPLAY labels — raw enum stays the
     *  filter/sort value. UNMAPPED degrades to the raw enum verbatim. */
    strategy: {
      Scalper: "短炒",
      "Day Trader": "當沖",
      Swing: "波段",
      Position: "部位",
      "Hold-to-Resolve": "持有至結算",
      Unknown: "未分類",
    },
    /** risk_flags codes — raw code stays the underlying value. */
    riskFlag: {
      insider_suspect: "疑似內線",
      strategy_drift: "策略漂移",
      new_account: "新帳號",
      low_liquidity: "低流動性",
      high_concentration: "高度集中",
    },
    /** candidate_leaders.sources tags. */
    sourceLabel: {
      leaderboard: "排行榜",
      hot_market: "熱門市場",
      large_new_positions: "大額新部位",
    },
    /** lifecycle_state Badge labels — the AUTOMATED ladder state column
     *  (discovered/candidate/paper/trial_ready/cooldown/retired). UNMAPPED
     *  falls back to the raw enum verbatim. */
    lifecycle: {
      discovered: "已發現",
      candidate: "候選",
      paper: "Paper 模擬中",
      trial_ready: "可試跟單",
      cooldown: "冷卻中",
      retired: "已淘汰",
      unknown: "未知",
    },
    /** Column headers. */
    columns: {
      wallet: "Wallet",
      profit: "獲利",
      replicability: "可複製性",
      total: "總分",
      lifecycle: "階梯狀態",
      copyability: "可複製性（Paper）",
      paperPnl: "Paper PnL",
      paperDays: "Paper 天數",
      paperTrades: "交易筆數",
      gate: "升級門檻進度",
      lastSeen: "最近資料",
      cooldownCount: "冷卻次數",
      createdAt: "進入時間",
      actions: "操作",
    },
    /** Reset filters toolbar control. */
    reset: "重設",
    /** Gate-progress hints (paper-state rows). */
    gate: {
      /** {{count}} = current trade_count, {{need}} = required (10). */
      trades: "{{count}}/{{need}} 筆交易",
      /** {{day}} = session age in days, {{need}} = required (7). */
      days: "第 {{day}} 天 / 共需 {{need}} 天",
      ready: "已達門檻",
      none: "無",
    },
    /** {{days}} = paper-session age in whole days. */
    daysValue: "{{days}} 天",
    /** Cell placeholder for a row with no paper_session. */
    noSession: "尚無 session",
    /** Per-row quick-action menu (kept: quick + custom backtest). */
    actions: {
      label: "操作",
      quickBacktest: "快速 Backtest（90 天 · Balanced）",
      customizeBacktest: "自訂 Backtest…",
    },
    /** Sonner toasts fired by row actions. */
    toast: {
      backtestQueued: "Backtest 已排入佇列（90 天 · Balanced）",
      backtestFailed: "Backtest 排入佇列失敗，請稍候重試。",
    },
    /** Last-seen freshness pill (relative — today / Nd / Nw / Nmo). */
    lastSeen: {
      today: "今天",
      daysAgo: "{{days}} 天前",
      weeksAgo: "{{weeks}} 週前",
      monthsAgo: "{{months}} 月前",
      never: "未知",
    },
    /** Promote-to-live action — kept identical to the legacy /research
     *  flow (danger-passphrase + paused follow_relations insert). */
    promote: {
      title: "升級為真實跟單",
      description:
        "將此 trial_ready 候選 Leader 升級為真實跟單。新跟單關係會以「暫停」狀態建立，需再於 /leaders 手動恢復後才會實際下單（雙重閘門）。",
      successToast: "已升級為暫停中的真實跟單，請至 /leaders 確認後恢復。",
      notReady: "未達 trial_ready",
      locked: "已鎖定",
      genericError: "升級失敗，未建立任何跟單關係。",
    },
    /** Empty states. */
    empty: {
      filteredTitle: "沒有符合篩選條件的 Leader。",
      filteredHint: "清除篩選條件即可看到完整候選名單。",
      clearFilters: "清除篩選",
      noDataTitle: "尚無候選者，請在 trader-core 主機執行 polyecho discovery scan。",
      noDataHint: "discovery 掃描找到 smart-money wallet 後，名單即會填入。",
      /** Per-chip empty hints. */
      noActive: "目前沒有候選 Leader 在驗證流程中。",
      noTrialReady: "目前沒有候選 Leader 已達 trial_ready。等待 paper-trading 完成驗證。",
      noRetired: "目前沒有已退休的候選 Leader。",
    },
    /** Pagination footer. */
    pagination: {
      /** {{first}}–{{last}} of {{total}} (numbers, not translated). */
      range: "{{first}}–{{last}} / 共 {{total}}",
      empty: "0 / 共 0",
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
    caption: "歷史總分變化，{{trend}}。",
  },
} as const;
