/** Tables group (`app/trades`, `app/positions`, `app/accounts`,
 *  `app/leaders` + their shared table components) zh-TW strings.
 *  Filled by the i18n swarm.
 *  Proper nouns stay English — see common.ts PROPER-NOUN POLICY. */
export const tables = {
  /** Cross-table cell fallbacks reused by every table in this group. */
  common: {
    notSet: "未設定",
    notRecorded: "未記錄",
    notYet: "尚未",
  },
  /** copy_skips.skip_reason taxonomy badge labels (frozen SkipReason
   *  enum; rendered by lib/skip-reasons via the skips table).
   *  `slippage`/`market` stay English per PROPER-NOUN POLICY. */
  skipReasons: {
    dup: "重複",
    notFollowing: "未跟隨",
    filterKeyword: "關鍵字過濾",
    filterPriceBand: "價格區間",
    filterMarketEnd: "market 即將結束",
    orderbookSpread: "價差過大",
    orderbookDepth: "深度不足",
    positionCap: "持倉上限",
    belowMinSize: "低於最小數量",
    dailyOrderCap: "單日下單上限",
    dailyLossCap: "單日虧損上限",
    portfolioCap: "投資組合上限",
    slippageReject: "slippage 拒單",
    strategyDrift: "策略偏移",
    dataUnavailable: "資料無法取得",
    liveLocked: "Live 已 LOCKED",
  },
  trades: {
    title: "交易",
    subtitleEmpty: "trader-core 執行的每一筆跟單交易都會記錄在這裡。",
    subtitleCount: "已記錄 {{count}} 筆跟單交易。",
    emptyTitle: "尚無跟單交易記錄。請先到 Leaders 頁跟隨一位 Leader；當 trader-core 為其執行一筆跟單後，就會出現在這裡。",
    col: {
      opened: "開倉時間",
      leader: "Leader",
      market: "Market",
      side: "方向",
      size: "數量",
      price: "價格",
      fill: "成交",
      pnl: "PnL",
    },
    searchPlaceholder: "輸入 market 名稱或關鍵字",
    filter: {
      fillStatus: "成交狀態",
      filled: "已成交",
      pending: "等待中",
      partial: "部分成交",
      rejected: "已拒絕",
    },
  },
  positions: {
    title: "持倉",
    subtitleEmpty: "未平倉的跟單持倉會即時顯示在這裡。",
    subtitleCount: "{{count}} 筆未平倉持倉。",
    emptyTitle: "無未平倉持倉。請先到 Leaders 頁跟隨一位 Leader；當 trader-core 為其開立一筆跟單後，就會出現在這裡。",
    col: {
      market: "Market",
      leader: "Leader",
      side: "方向",
      size: "數量",
      entry: "進場價",
      matched: "已撮合",
      remaining: "剩餘",
      fill: "成交",
      pnl: "PnL",
      opened: "開倉時間",
    },
    searchPlaceholder: "輸入 market 名稱或關鍵字",
    filter: {
      fillStatus: "成交狀態",
      filled: "已成交",
      pending: "等待中",
      partial: "部分成交",
      rejected: "已拒絕",
    },
  },
  accounts: {
    title: "帳戶",
    subtitleEmpty: "你註冊的 Polymarket 帳戶（CLI 或下方 Web 匯入）。",
    subtitleCount: "{{count}} 個已註冊帳戶。",
    cliOnlyBadge: "CLI / Web",
    cliOnlyTitle: "建議用 CLI 匯入私鑰。",
    cliOnlyBody:
      "建議在 trader-core 主機上執行 {{cmd}}。亦可用下方「從 Web 匯入私鑰」（操作員明確授權的覆寫，已記錄）：私鑰僅以 AES-256-GCM 加密儲存，永不顯示或記錄，並需通過危險操作確認密碼。",
    emptyPrefix:
      "尚未註冊任何帳戶。請用 CLI 執行 {{cmd}}，或用下方「從 Web 匯入私鑰」。",
    col: {
      name: "名稱",
      eoaAddress: "EOA 位址",
      signatureType: "簽章類型",
      status: "狀態",
    },
    copied: "已複製",
    copyAddress: "複製位址",
    /** Operator-authorised web key-import override (recorded). The default
     *  model is CLI-only; the operator knowingly enabled this. Proper nouns
     *  (EOA / funder / AES-256-GCM / CLI) stay English per PROPER-NOUN
     *  POLICY. */
    import: {
      heading: "從 Web 匯入私鑰",
      intro:
        "預設模式為僅限 CLI 匯入；此功能為操作員明確授權的覆寫（已記錄）。私鑰會以 AES-256-GCM 加密儲存，永不顯示或記錄。需通過危險操作確認密碼。",
      openButton: "匯入私鑰",
      cancel: "取消",
      nameLabel: "帳戶名稱（選填）",
      namePlaceholder: "預設為 EOA 位址",
      eoaLabel: "EOA 位址",
      eoaPlaceholder: "0x… 40 個十六進位字元",
      sigTypeLabel: "簽章類型",
      sigType0: "0 · EOA（funder 即簽章者）",
      sigType1: "1 · POLY_PROXY（需顯式 funder）",
      sigType2: "2 · POLY_GNOSIS_SAFE（需顯式 funder）",
      sigType3: "3 · POLY_1271（需顯式 funder）",
      funderLabel: "Funder 位址",
      funderPlaceholder: "0x… 40 個十六進位字元",
      funderHintType0: "type 0：funder 自動等於 EOA，無需填寫。",
      funderHintOther:
        "type 1/2/3：請填入經鏈上驗證的 funder 位址。type 1 的 CREATE2 推導僅 CLI 端有可信實作，本頁不臆測。",
      privateKeyLabel: "私鑰",
      privateKeyPlaceholder: "0x… 64 個十六進位字元",
      privateKeyHint:
        "提交後以 AES-256-GCM 加密儲存，永不顯示或記錄；密文與 crypto.py v1 完全相容。",
      encPassHint:
        "trader-core 解密私鑰時使用的密語；伺服器不留存明文。請與 CLI 端佈建的值一致。",
      dangerPassHint:
        "需先由 CLI set-danger-passphrase 佈建。伺服器以常數時間比對雜湊；錯誤時不寫入任何資料。",
      encPassLabel: "私鑰加密密語",
      encPassPlaceholder: "輸入密語",
      dangerPassLabel: "危險操作確認密碼",
      dangerPassPlaceholder: "輸入危險操作密碼",
      submit: "加密並匯入",
      submitting: "匯入中…",
      errEoa: "EOA 位址格式無效（需為 0x 加 40 個十六進位字元）。",
      errPrivateKey: "私鑰格式無效（需為 64 個十六進位字元，可選 0x 前綴）。",
      errEncPass: "請輸入私鑰加密密語。",
      errDangerPass: "請輸入危險操作確認密碼。",
      errFunder: "Funder 位址格式無效（需為 0x 加 40 個十六進位字元）。",
      errFailed: "匯入失敗，未寫入任何資料。請稍候重試。",
      /** Signature type is deliberately one of the LAST steps — an
       *  informed choice after the operator has seen the balances. */
      sigTypeHint:
        "請先確認上方 EOA（與 funder）餘額，再於此選擇簽章類型——這是知情後的選擇，而非預先盲選。",
      /** Read-only, best-effort balance line for the address we ALREADY
       *  have (EOA, or an explicit operator funder). It is NOT a full
       *  detect — that stays the CLI `polyecho account detect`. The
       *  composite probe shows three labelled lines: 部位 / 閒置 / 合計. */
      balanceLabel: "Polymarket 倉位價值",
      balanceProbing: "偵測中…",
      balanceFailed: "偵測失敗，可改用 CLI polyecho account detect",
      balanceEoaNote: "（簽章類型 0 時，此即帳戶價值：EOA 同時為 funder）",
      balanceFunderNote: "（此為所填 funder 位址的倉位價值）",
      balanceCliHint:
        "本頁僅顯示所持位址（EOA 或操作員填入的 funder）的餘額。完整四種簽章類型偵測（含推導的 funder）請於 trader-core 主機執行 CLI：polyecho account detect <EOA>。",
      balancePositionsLabel: "部位",
      balanceIdleLabel: "閒置 USDC",
      balanceTotalLabel: "合計",
      balanceNa: "N/A",
      balanceIdleFailed: "鏈上 USDC 餘額讀取失敗",
      balancePartialFailed: "部分餘額讀取失敗（仍可送出）",
      /** P2 — sig-type-1 CREATE2 cross-check (warn-only; never blocks). */
      create2Probing: "比對 CREATE2 推導中…",
      create2Match: "✓ 與 CREATE2 推導相符",
      create2MismatchHeading:
        "⚠ 你填入的 funder 與 CREATE2 推導不符",
      create2MismatchHint:
        "可能是有意的（例如自訂代理 / 已遷移帳戶）；若你不確定，請對照 polymarket.com/settings 顯示的 funder。本頁不阻擋送出。",
      create2DerivedLabel: "CREATE2 推導",
    },
  },
  leaders: {
    title: "Leaders",
    subtitleEmpty: "你跟單的 Leader 以及各跟單所使用的範本。",
    subtitleCount: "{{count}} 組跟單關係 · {{active}} 組進行中。",
    emptyPrefix: "尚未跟隨任何人。請從",
    emptyDiscovery: "探索",
    emptyMiddle: "推薦一個候選者，再從 CLI 以",
    emptySuffix: "開始跟單。",
    col: {
      leader: "Leader",
      account: "帳戶",
      template: "範本",
      status: "狀態",
      lastSeen: "最後出現",
      followed: "跟單時間",
      action: "操作",
    },
    pause: "暫停",
    resume: "恢復",
    pausing: "暫停中…",
    pauseAria: "暫停跟單 {{address}}",
    pausedToast: "已暫停 Leader。trader-core 將停止對其跟單。",
    resumedToast: "已恢復 Leader。下一筆 Leader 交易將繼續跟單。",
    updateError: "更新跟單狀態失敗，請稍候重試。",
    /** P7d / R24 inline-confirm description for resume.
     *  {{address}} = the leader's wallet address (literal, not translated). */
    resumeConfirmDescription:
      "這會恢復對 Leader {{address}} 的跟單。trader-core 將於下一筆 Leader 交易繼續跟。請完成二次確認後送出。",
    skips: {
      heading: "近期略過",
      subtitleEmpty:
        "當 trader-core 評估一筆 Leader 交易後，略過跟單的原因會顯示在這裡。",
      subtitleLast: "最近 {{limit}} 筆略過，由新到舊。唯讀。",
      subtitleCount: "{{count}} 筆略過，由新到舊。唯讀。",
      emptyTitle:
        "尚無略過記錄。trader-core 會為每一筆被略過或拒絕的跟單記錄一列，並附上分類原因。",
      col: {
        reason: "原因",
        leader: "Leader",
        condition: "Condition",
        detail: "詳情",
        when: "時間",
      },
    },
  },
} as const;
