/** Backtest group (`app/backtest/new/page.tsx`, `app/backtest/[id]/
 *  page.tsx`) zh-TW strings. Filled by the i18n swarm.
 *  Proper nouns stay English — see common.ts PROPER-NOUN POLICY.
 *  (PnL / USD / Leader / Sharpe / bps / backtest 等技術詞保留英文) */
export const backtest = {
  new: {
    title: "New Backtest",
    desc: "設定一個 backtest 任務。送出佇列後，worker 會接手處理，結果會顯示在它自己的頁面。",
  },
  result: {
    back: "← New Backtest",
    title: "Backtest 結果",
    job: "任務：{{id}}",
    leader: "Leader：",
    status: {
      pending: "已排入佇列",
      queued: "已排入佇列",
      running: "執行中",
      completed: "已完成",
      failed: "失敗",
    },
    params: {
      title: "任務參數",
      period: "期間",
      capital: "本金",
      slippage: "Slippage",
      delay: "延遲",
      slippageUnit: "{{value}} bps",
      delayUnit: "{{value}} 秒",
    },
    pending: {
      title: "已排入佇列",
      body: "已排入佇列，backtest worker 將會處理此任務。本頁每 10 秒自動重新整理一次，你也可以手動重新載入。",
    },
    running: {
      title: "執行中",
      body: "本頁每 10 秒自動重新整理一次，你也可以手動重新載入。",
    },
    failed: {
      title: "Backtest 失敗",
      bodyPrefix: "backtest 引擎回傳錯誤。請以 ",
      bodySuffix: " 檢視 Python worker 紀錄，然後重新將任務送入佇列。",
    },
    scenario: {
      title: "情境比較",
      desc: "在不同的 slippage 與延遲假設下的最佳、基準與最差情況。",
      labels: {
        optimistic: "樂觀",
        neutral: "中性",
        pessimistic: "悲觀",
      },
      paramsCol: "{{slippage}} bps / {{delay}} 秒",
    },
    pnlCurves: "PnL 曲線（多情境）",
    pnlCurve: "PnL 曲線",
    statsTitle: "統計",
    table: {
      metric: "指標",
      value: "數值",
      totalReturn: "總報酬",
      annualized: "年化報酬",
      sharpe: "Sharpe",
      maxDrawdown: "最大回撤",
      winRate: "勝率",
      avgHold: "平均持有",
      trades: "交易筆數",
      skipped: "略過筆數",
      avgHoldUnit: "{{value}} 小時",
    },
  },
  /** `app/backtest/new/backtest-form.tsx` — shadcn form (RHF + zod).
   *  Zod 訊息也走 i18n；技術詞（wallet / 0x / copy_ratio / fixed_size_usd /
   *  start_date / end_date / bps / USD / slippage / Leader）依 common.ts
   *  PROPER-NOUN POLICY 保留英文。 */
  form: {
    /** Zod 驗證訊息。 */
    validation: {
      walletInvalid: "請輸入有效的 wallet 位址",
      walletPrefix: "必須以 0x 開頭",
      dateFormat: "YYYY-MM-DD",
      capitalMin: "最低 $10",
      sizingRequired: "請擇一填寫 copy_ratio 或 fixed_size_usd",
      dateOrder: "start_date 必須早於 end_date",
    },
    sections: {
      leaderDates: {
        title: "Leader 與日期區間",
        hint: "要鏡像哪一個 wallet，以及要重播的時間區間。",
      },
      capital: {
        title: "本金與部位大小",
        hint: "起始本金，以及每筆跟單的部位計算方式。請設定 copy ratio 或固定金額其中一項，不可兩者皆填。",
      },
      execution: {
        title: "執行模型",
        hint: "套用到每一筆模擬交易的 slippage 與成交延遲。",
      },
      scenario: {
        title: "情境",
        hint: "執行單一執行模型，或同時並排跑三種情境。",
      },
    },
    fields: {
      leaderAddress: {
        label: "Leader 位址",
        placeholder: "0x…",
        help: "要 backtest 的 leader wallet 位址",
      },
      startDate: { label: "起始日期" },
      endDate: { label: "結束日期" },
      initialCapital: { label: "起始本金（USD）" },
      copyRatio: {
        label: "Copy Ratio",
        placeholder: "0.001",
        help: "例如 0.001 = 0.1%",
      },
      fixedSize: {
        label: "固定金額（USD）",
        placeholder: "留空則改用 copy ratio",
        help: "每筆交易固定的金額",
      },
      maxPosition: { label: "最大部位（USD）" },
      minTradeSize: { label: "最小交易金額（USD）" },
      dailyLossLimit: {
        label: "每日虧損上限（USD）",
        help: "當日虧損超過此上限後即停止跟單",
      },
      slippage: {
        label: "Slippage（bps）",
        help: "150 bps = 1.5%",
      },
      executionDelay: {
        label: "執行延遲（秒）",
        help: "模擬的成交延遲",
      },
      categoryWhitelist: {
        label: "類別白名單",
        placeholder: "Politics, Sports, Economy",
        help: "以逗號分隔。留空則納入所有類別。",
      },
    },
    runMode: {
      label: "執行模式",
      placeholder: "選擇情境",
      triple: "Triple（全部 3 種情境，建議）",
      optimistic: "樂觀（50 bps slippage，10 秒延遲）",
      neutral: "中性（150 bps，30 秒）",
      pessimistic: "悲觀（300 bps，60 秒）",
      help: "「Triple」會一次跑完三種情境，方便比較最佳、基準與最差情況（PRD §5.4）。",
    },
    submitHint: "送出此任務佇列。worker 會在稍後接手處理。",
    submit: "送出 Backtest",
    submitting: "送出中…",
    toastQueued: "Backtest 已排入佇列，即將跳轉…",
    toastError: "Backtest 送入佇列失敗",
  },
} as const;
