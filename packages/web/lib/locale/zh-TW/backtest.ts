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
} as const;
