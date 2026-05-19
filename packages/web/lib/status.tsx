/**
 * /status presentation contract — the SINGLE source of truth for how a
 * worker-liveness verdict or an integration-probe verdict is rendered.
 *
 * DESIGN.md "Color never the sole signal" / accessibility: every state is
 * conveyed by THREE redundant channels — a shadcn Badge variant, a Lucide
 * icon, and a plain text label. A colour-blind operator (or a greyscale
 * screenshot) reads the icon + label; colour is reinforcement only. The
 * page and the cross-language contract test both import this map so the
 * rendered verdict can never drift from what the test asserts.
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

export interface StatusPresentation {
  /** Plain text label — the primary, never-color-only signal. */
  label: string;
  /** shadcn Badge variant (reinforcement). */
  variant: NonNullable<BadgeProps["variant"]>;
  /** Lucide glyph (reinforcement; shape differs per state, not just hue). */
  Icon: LucideIcon;
  /** Stable hook for tests / e2e — the raw verdict string. */
  token: string;
}

/**
 * Worker liveness → presentation.
 *
 *  running → "Running"          secondary  CheckCircle2
 *  stale   → "Stale (> 5 min)"  destructive AlertTriangle
 *  missing → "No data"          outline     CircleSlash   (NOT a failure —
 *            PolyHermes "zero vs unavailable": no row ≠ a down worker)
 */
export function workerPresentation(
  liveness: HeartbeatLiveness
): StatusPresentation {
  switch (liveness) {
    case "running":
      return {
        label: "Running",
        variant: "secondary",
        Icon: CheckCircle2,
        token: "running",
      };
    case "stale":
      return {
        label: `Stale (> ${HEARTBEAT_STALE_MINUTES} min)`,
        variant: "destructive",
        Icon: AlertTriangle,
        token: "stale",
      };
    default:
      return {
        label: "No data",
        variant: "outline",
        Icon: CircleSlash,
        token: "missing",
      };
  }
}

/**
 * Integration-probe verdict → presentation.
 *
 *  ok   → "OK"    secondary   CheckCircle2
 *  down → "Down"  destructive XCircle      (a REAL failure, distinct from a
 *         worker's "No data" — different icon + label so the two never read
 *         the same even in greyscale)
 */
export function probePresentation(
  verdict: ProbeVerdict
): StatusPresentation {
  if (verdict === "ok") {
    return {
      label: "OK",
      variant: "secondary",
      Icon: CheckCircle2,
      token: "ok",
    };
  }
  return {
    label: "Down",
    variant: "destructive",
    Icon: XCircle,
    token: "down",
  };
}

/** A coerced-but-absent integration_status row → an honest "no data" badge. */
export function integrationNoData(): StatusPresentation {
  return {
    label: "No data",
    variant: "outline",
    Icon: CircleSlash,
    token: "missing",
  };
}

/** Human label for a worker heartbeat key (frozen key → instrument name). */
export function workerLabel(key: string): string {
  switch (key) {
    case "trader_heartbeat":
      return "Trader";
    case "discovery_heartbeat":
      return "Discovery";
    case "backtest_heartbeat":
      return "Backtest";
    case "push_heartbeat":
      return "Push notifier";
    default:
      return key;
  }
}

/** Human label for a known integration-probe key (frozen probe → name). */
export function probeLabel(key: string): string {
  switch (key) {
    case "config":
      return "Config";
    case "database":
      return "Database";
    case "polymarket_clob":
      return "Polymarket CLOB";
    case "polymarket_data_api":
      return "Polymarket Data API";
    case "polygon_rpc":
      return "Polygon RPC";
    case "vpn_egress":
      return "VPN egress";
    default:
      // Unknown future probe: humanise the snake_case key, never echo raw.
      return key
        .replace(/_/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase());
  }
}
