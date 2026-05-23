/**
 * Inline-confirm primitive (`components/inline-confirm.tsx`) zh-TW strings.
 *
 * Shared by the RED Server Actions in P7d (R24 design intent):
 *   1. setGlobalPaused(false)   resume kill-switch  (REQUIRES passphrase)
 *   2. setFollowStatus("active") resume one leader
 *   3. setRiskMode("normal")     leave conservative
 *   4. setPolymarketProxy(new)   set/change proxy
 *
 * The legacy 5th hook (`updateLeaderStatus("following")` promote-to-follow)
 * was removed when the `candidate_leaders.user_status` column was dropped.
 *
 * Safe-direction (pause / conservative / clear / etc.) is single-click and
 * does NOT use this primitive — see DESIGN.md "calm instrument chrome".
 *
 * Proper nouns stay English (CLI, trader-core, Polymarket, proxy) — see
 * common.ts PROPER-NOUN POLICY.
 */
export const inlineConfirm = {
  /** {{title}} = caller-provided operation label (e.g. "恢復全部跟單"). */
  formAria: "確認執行：{{title}}",
  /** {{title}} = caller-provided operation label. */
  sectionTitle: "二次確認：{{title}}",
  intro:
    "此操作會放鬆風險閘門。請於下方就地完成二次確認後送出；錯誤時不寫入任何資料（伺服器以常數時間比對通行碼）。",
  /** {{phrase}} = the verbatim confirm phrase (literal, not translated). */
  confirmPhraseLabel: "請完整輸入「{{phrase}}」",
  /** Placeholder echoes the phrase byte-for-byte. */
  confirmPhrasePlaceholder: "確認執行",
  passphraseLabel: "危險操作通行碼",
  passphrasePlaceholder: "輸入通行碼",
  passphraseHint:
    "需先由 CLI set-danger-passphrase 佈建；伺服器以常數時間比對雜湊，錯誤時不寫入任何資料。",
  /** {{title}} = caller-provided operation label. */
  openButton: "{{title}}…",
  submitButton: "確認並執行",
  submitting: "送出中…",
  cancelButton: "取消",
  /** {{title}} = caller-provided operation label. */
  openAria: "展開二次確認：{{title}}",
  /** {{title}} = caller-provided operation label. */
  submitAria: "確認並執行：{{title}}",
  /** {{phrase}} = the verbatim confirm phrase (literal, not translated). */
  errConfirmPhrase: "請完整輸入「{{phrase}}」以確認此操作。",
  errPassphrase: "請輸入危險操作通行碼。",
  errSubmitFailed: "送出失敗，未執行任何操作。請稍候重試。",
} as const;
