/**
 * Research group (`app/research/page.tsx`) zh-TW strings — C1-P5.
 *
 * The /research page surfaces the C1 forward paper-trading ladder: each
 * candidate leader's lifecycle_state + its paper_session stats, plus the
 * operator-gated "promote to live follow" action.
 *
 * Proper nouns stay English — see common.ts PROPER-NOUN POLICY
 * (Leader / PnL / paper / trial_ready / follow 等技術詞保留英文).
 */
export const research = {
  title: "Paper-Trading 階梯",
  subtitleEmpty: "尚無候選 Leader 進入 paper-trading 階梯。",
  /** {{count}} = total candidates, {{ready}} = trial_ready count. */
  subtitleCount: "{{count}} 位候選 Leader · {{ready}} 位已達 trial_ready。",

  /** Calm advisory banner above the table — money-safety boundary. */
  boundary:
    "trial_ready 僅為建議。階梯永遠不會自動建立真實跟單；唯一路徑是下方經危險通行碼確認的「升級為真實跟單」，且升級後的跟單會以暫停狀態建立，需再於 /leaders 手動恢復。",

  empty: {
    title: "尚無 paper-trading 紀錄。",
    hint: "候選 Leader 達 score ≥ 60 後會由 paper worker 自動開啟 paper-trading session。",
  },

  /** Lifecycle-state Badge labels. lifecycle_state stays the canonical
   *  English enum on the wire; these are the zh-TW display labels. */
  state: {
    discovered: "已發現",
    candidate: "候選",
    paper: "Paper 模擬中",
    trial_ready: "可試跟單",
    cooldown: "冷卻中",
    retired: "已淘汰",
    unknown: "未知",
  },

  col: {
    leader: "Leader",
    state: "階梯狀態",
    paperPnl: "Paper PnL",
    paperDays: "Paper 天數",
    trades: "交易筆數",
    gate: "升級門檻進度",
    action: "操作",
  },

  /** Gate-progress hints shown in the col.gate cell for a paper-state row. */
  gate: {
    /** {{count}} = current trade_count, {{need}} = required (10). */
    trades: "{{count}}/{{need}} 筆交易",
    /** {{day}} = session age in days, {{need}} = required (7). */
    days: "第 {{day}} 天 / 共需 {{need}} 天",
    /** Shown when a paper row already clears every gate. */
    ready: "已達門檻",
    /** Shown for a non-paper row (no open session to gate). */
    none: "無",
  },

  /** {{days}} = paper-session age in whole days. */
  daysValue: "{{days}} 天",
  /** Placeholder for a row with no paper_session. */
  noSession: "尚無 session",

  /** Client-side table pagination (50 rows per page). */
  pagination: {
    /** {{count}} = total candidate rows across all pages. */
    total: "共 {{count}} 筆",
    prev: "上一頁",
    next: "下一頁",
    /** {{page}} = current page, {{total}} = total pages. */
    pageOf: "第 {{page}} / {{total}} 頁",
  },

  promote: {
    /** InlineConfirm trigger label ({{title}} interpolation in the primitive). */
    title: "升級為真實跟單",
    /** Confirm-form description text passed to InlineConfirm. */
    description:
      "將此 trial_ready 候選 Leader 升級為真實跟單。新跟單關係會以「暫停」狀態建立，需再於 /leaders 手動恢復後才會實際下單（雙重閘門）。",
    successToast: "已升級為暫停中的真實跟單，請至 /leaders 確認後恢復。",
    /** Shown in the action cell for a non-trial_ready row. */
    notReady: "未達 trial_ready",
    /** Shown in the action cell for a locked candidate. */
    locked: "已鎖定",
    /** Client-side fallback when the Server Action throws (not its
     *  fail-closed `{ok:false}` envelope, which carries its own zh-TW). */
    genericError: "升級失敗，未建立任何跟單關係。",
  },
} as const;
