/**
 * /status presentation contract — the SINGLE source of truth for how a
 * worker-liveness verdict or an integration-probe verdict is rendered.
 *
 * DESIGN.md "Color never the sole signal" / accessibility: every state is
 * conveyed by FOUR redundant channels — a plain text label (load-bearing),
 * a Lucide icon (shape differs per state, not just hue), a shadcn Badge
 * variant, and a calm semantic `tone`. A colour-blind operator (or a
 * greyscale screenshot) reads the icon + label alone; the tone is pure
 * reinforcement. The page and the cross-language contract test both import
 * this map so the rendered verdict can never drift from what the test
 * asserts.
 *
 * The `tone` colour system (resolved to a soft-tint pill by <StatusBadge/>,
 * tokens in app/globals.css `--status-*`). Three verbs, three calm hues,
 * tinted toward the brand cool-azure family, chroma pulled down at the
 * extremes so /status reads as a quiet instrument, never an alarm panel:
 *
 *   "ok"      running / probe-OK  — healthy; gain-adjacent green, but
 *                                   calmer than PnL --gain (liveness ≠ money)
 *   "warn"    stale (> 5 min)     — a WARNING, not a failure; a distinct
 *                                   amber so it can NEVER read as `down`
 *                                   (fixes stale==down colour collision)
 *   "down"    probe DOWN          — a real failure; loss/destructive family
 *   "neutral" missing / no-data   — "no data ≠ down" (PolyHermes zero-vs-
 *                                   unavailable): muted, NOT a colour alarm
 */

import {
  CheckCircle2,
  AlertTriangle,
  CircleSlash,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import type { BadgeProps } from "@/components/ui/badge";
import type { HeartbeatLiveness } from "@/lib/settings-keys";
import { type ProbeVerdict, HEARTBEAT_STALE_MINUTES } from "@/lib/settings-keys";
import { t } from "@/lib/i18n";

/**
 * Semantic colour family for a verdict (the calm /status palette). Resolved
 * to a concrete soft-tint pill by <StatusBadge/>; the underlying tokens live
 * in app/globals.css (`--status-ok|warn|down*`, tuned for light AND dark).
 * Colour is the LAST of the four channels — pure reinforcement, never alone.
 */
export type StatusTone = "ok" | "warn" | "down" | "neutral";

export interface StatusPresentation {
  /** Plain text label — the primary, never-color-only signal. */
  label: string;
  /** shadcn Badge variant (reinforcement). */
  variant: NonNullable<BadgeProps["variant"]>;
  /** Lucide glyph (reinforcement; shape differs per state, not just hue). */
  Icon: LucideIcon;
  /** Calm semantic colour family (reinforcement; resolved by StatusBadge). */
  tone: StatusTone;
  /** Stable hook for tests / e2e — the raw verdict string. */
  token: string;
}

/**
 * Worker liveness → presentation.
 *
 *  running → "Running"          secondary  CheckCircle2  tone ok
 *  stale   → "Stale (> 5 min)"  destructive AlertTriangle tone warn
 *  missing → "No data"          outline     CircleSlash   tone neutral
 *            (NOT a failure — PolyHermes "zero vs unavailable": no row ≠ a
 *            down worker; stays neutral, never a colour alarm)
 *
 * The Badge `variant` is the FROZEN cross-stack contract channel (the test
 * pins it); the calm semantic colour rides the separate `tone` channel so
 * `stale` finally reads amber (a warning) instead of sharing `down`'s red.
 */
export function workerPresentation(
  liveness: HeartbeatLiveness
): StatusPresentation {
  switch (liveness) {
    case "running":
      return {
        label: t("status.verdict.running"),
        variant: "secondary",
        Icon: CheckCircle2,
        tone: "ok",
        token: "running",
      };
    case "stale":
      return {
        label: t("status.verdict.stale", {
          minutes: HEARTBEAT_STALE_MINUTES,
        }),
        variant: "destructive",
        Icon: AlertTriangle,
        tone: "warn",
        token: "stale",
      };
    default:
      return {
        label: t("status.verdict.noData"),
        variant: "outline",
        Icon: CircleSlash,
        tone: "neutral",
        token: "missing",
      };
  }
}

/**
 * Integration-probe verdict → presentation.
 *
 *  ok   → "OK"    secondary   CheckCircle2  tone ok
 *  down → "Down"  destructive XCircle       tone down  (a REAL failure,
 *         distinct from a worker's "No data" — different icon + label + tone
 *         so the two never read the same even in greyscale)
 */
export function probePresentation(
  verdict: ProbeVerdict
): StatusPresentation {
  if (verdict === "ok") {
    return {
      label: t("status.verdict.ok"),
      variant: "secondary",
      Icon: CheckCircle2,
      tone: "ok",
      token: "ok",
    };
  }
  return {
    label: t("status.verdict.down"),
    variant: "destructive",
    Icon: XCircle,
    tone: "down",
    token: "down",
  };
}

/** A coerced-but-absent integration_status row → an honest "no data" badge.
 *  Neutral tone on purpose: an absent probe row is "unavailable", NOT a
 *  failure (PolyHermes zero-vs-unavailable) — it must not flash `down`'s red. */
export function integrationNoData(): StatusPresentation {
  return {
    label: t("status.verdict.noData"),
    variant: "outline",
    Icon: CircleSlash,
    tone: "neutral",
    token: "missing",
  };
}

/**
 * Defensive length cap for the worker-written `phase` raw string. It
 * renders inline on /status; React escapes the text (no XSS) but a
 * pathological / rogue DB value would blow out the layout (a view-DoS).
 * Mirrors lib/alerts.ts `cappedFallback` (FALLBACK_MAX 40) — a normal
 * phase value passes through untouched, only an oversize one is truncated.
 * (probe `detail` is already capped upstream in lib/settings-keys
 * `coerceProbe`; `phase` had no cap, hence this.)
 */
const STATUS_FIELD_MAX = 40;

export function cappedField(raw: string): string {
  const s = typeof raw === "string" ? raw : String(raw ?? "");
  return s.length > STATUS_FIELD_MAX
    ? `${s.slice(0, STATUS_FIELD_MAX)}…`
    : s;
}

/** Human label for a worker heartbeat key (frozen key → instrument name). */
export function workerLabel(key: string): string {
  switch (key) {
    case "trader_heartbeat":
      return t("status.workerLabel.trader");
    case "discovery_heartbeat":
      return t("status.workerLabel.discovery");
    case "backtest_heartbeat":
      return t("status.workerLabel.backtest");
    case "push_heartbeat":
      return t("status.workerLabel.push");
    default:
      return key;
  }
}

/** Human label for a known integration-probe key (frozen probe → name). */
export function probeLabel(key: string): string {
  switch (key) {
    case "config":
      return t("status.probeLabel.config");
    case "database":
      return t("status.probeLabel.database");
    case "polymarket_clob":
      return t("status.probeLabel.polymarketClob");
    case "polymarket_data_api":
      return t("status.probeLabel.polymarketDataApi");
    case "polygon_rpc":
      return t("status.probeLabel.polygonRpc");
    case "vpn_egress":
      return t("status.probeLabel.vpnEgress");
    case "polymarket_ws_user":
      return t("status.probeLabel.polymarketWsUser");
    case "polymarket_ws_market":
      return t("status.probeLabel.polymarketWsMarket");
    default:
      // Unknown future probe: humanise the snake_case key, never echo raw.
      return key
        .replace(/_/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase());
  }
}
