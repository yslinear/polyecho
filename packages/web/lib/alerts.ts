/**
 * Display helpers for the /alerts 警示中心.
 *
 * EventType / PendingActionType string values are the frozen contract from
 * p5-core (PRD §5.8 / §5.7). These maps are display-only; nothing here
 * writes or interprets semantics — the web never emits events.
 */

export type Severity = "info" | "warning" | "critical";

/** Humanised 台灣繁體中文 label per frozen EventType string. */
const EVENT_LABELS: Record<string, string> = {
  copy_fill: "跟單成交",
  position_closed: "部位平倉",
  stop_loss: "停損觸發",
  target_hit: "停利達標",
  daily_pnl_summary: "每日損益摘要",
  risk_daily_loss: "風控：當日虧損上限",
  risk_strategy_drift: "風控：策略偏移",
  system_api_failure: "系統：API 連線失敗",
  system_bot_restart: "系統：機器人重啟",
  system_low_balance: "系統：餘額偏低",
  candidate_high_score: "新發現高分候選",
  pending_dangerous_action: "待確認的危險操作",
};

/**
 * rev-web R18 C2: cap an UNKNOWN fallback string. These labels render
 * inline in the /alerts table; React already escapes the text (no XSS), but
 * a rogue/huge DB `event_type`/`action_type` string would blow out the
 * layout (a view-DoS). Unknown values get a generic prefix + a hard length
 * cap so a pathological row degrades gracefully instead of breaking the UI.
 */
const FALLBACK_MAX = 40;

function cappedFallback(raw: string): string {
  const s = typeof raw === "string" ? raw : String(raw ?? "");
  return s.length > FALLBACK_MAX
    ? `未知類型：${s.slice(0, FALLBACK_MAX)}…`
    : `未知類型：${s}`;
}

export function eventTypeLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? cappedFallback(eventType);
}

const PENDING_ACTION_LABELS: Record<string, string> = {
  manual_sell: "手動賣出",
  emergency_liquidate: "緊急清倉",
};

export function pendingActionLabel(actionType: string): string {
  return PENDING_ACTION_LABELS[actionType] ?? cappedFallback(actionType);
}

/** Severity badge variant + 繁中 label. Calm by default; critical is loud. */
export function severityMeta(severity: string): {
  label: string;
  /** maps to <Badge variant> */
  variant: "secondary" | "outline" | "destructive";
} {
  switch (severity) {
    case "critical":
      return { label: "CRITICAL", variant: "destructive" };
    case "warning":
      return { label: "WARNING", variant: "outline" };
    default:
      return { label: "INFO", variant: "secondary" };
  }
}

/** Compact one-line summary of an event's payload.data jsonb (best effort). */
export function summarisePayload(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const data = (payload as { data?: unknown }).data;
  if (data == null) return "";
  if (typeof data === "string") return data;
  if (typeof data === "number" || typeof data === "boolean") {
    return String(data);
  }
  if (typeof data === "object") {
    const parts: string[] = [];
    for (const [k, v] of Object.entries(data as Record<string, unknown>)) {
      if (v == null) continue;
      if (typeof v === "object") continue;
      parts.push(`${k}: ${String(v)}`);
      if (parts.length >= 4) break;
    }
    return parts.join(" · ");
  }
  return "";
}

/** Human countdown e.g. "4m 12s" / "已逾時" from now -> expires_at. */
export function timeRemaining(
  expiresAt: Date | string | null | undefined,
  now: Date = new Date()
): string {
  if (!expiresAt) return "無期限";
  const exp = expiresAt instanceof Date ? expiresAt : new Date(expiresAt);
  if (Number.isNaN(exp.getTime())) return "無期限";
  const ms = exp.getTime() - now.getTime();
  if (ms <= 0) return "已逾時";
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`;
}
