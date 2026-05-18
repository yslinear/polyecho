import { describe, it, expect } from "vitest";

import {
  coerceHeartbeat,
  heartbeatLiveness,
  HEARTBEAT_STALE_MINUTES,
} from "@/lib/settings-keys";

/**
 * rev-web R19 — CROSS-STACK contract guard for `settings.trader_heartbeat`.
 *
 * The heartbeat shape is DUPLICATED across two languages with no shared
 * codegen: the Python producer and the TS consumer must agree byte-for-byte
 * or the Dashboard liveness pill silently lies. This test pins the EXACT
 * JSON the Python side writes and asserts the TS coercion + liveness verdict
 * behave correctly on it.
 *
 * Authoritative producer (read-only reference, do NOT import — different
 * runtime):
 *
 *   packages/core/src/polyecho/core/trader.py :: Trader._write_heartbeat
 *     info = {
 *       "ts": datetime.now(tz=UTC).isoformat(),   # ISO-8601 w/ +00:00 offset
 *       "phase": phase,                           # e.g. "cycle_complete"
 *       "datasource": self._settings.datasource,
 *       "live_trading": is_live_enabled(),        # bool
 *       "detected": self.stats.detected,          # int
 *       "copied": self.stats.copied,              # int
 *       "skipped": self.stats.skipped,            # int
 *       "placed": self.stats.placed,              # int
 *       "followed_leaders": sorted({...lower()}), # list[str]
 *     }
 *
 *   packages/core/src/polyecho/core/wiring.py :: make_heartbeat_writer
 *     upserts `settings(key="trader_heartbeat", value=info)` — `info` is
 *     stored BARE as the JSONB value (not wrapped in {"value": ...}); the TS
 *     `coerceHeartbeat` tolerates BOTH bare and {value:...}.
 *
 * `datetime.isoformat()` for a tz-aware UTC datetime renders e.g.
 * `2026-05-18T07:30:00+00:00` (or with microseconds:
 * `2026-05-18T07:30:00.123456+00:00`). Both must parse via `new Date()`.
 */

/** Build the EXACT dict Python `_write_heartbeat` produces, given a ts. */
function pythonHeartbeatPayload(tsIso: string): Record<string, unknown> {
  return {
    ts: tsIso,
    phase: "cycle_complete",
    datasource: "polymarket_ws",
    live_trading: false,
    detected: 12,
    copied: 3,
    skipped: 9,
    placed: 3,
    followed_leaders: [
      "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ],
  };
}

/** Python `datetime.now(tz=UTC).isoformat()` shapes (with/without micros). */
function isoWithOffset(d: Date, withMicros: boolean): string {
  const base = d.toISOString().replace("Z", ""); // 2026-05-18T07:30:00.000
  const noMs = base.slice(0, 19); // strip ".000"
  return withMicros ? `${noMs}.123456+00:00` : `${noMs}+00:00`;
}

describe("R19 cross-stack: settings.trader_heartbeat Python↔TS contract", () => {
  it("parses the EXACT bare Python payload to a typed heartbeat", () => {
    const now = new Date();
    const payload = pythonHeartbeatPayload(isoWithOffset(now, false));

    // Stored BARE (make_heartbeat_writer stores `info` verbatim as JSONB).
    const hb = coerceHeartbeat(payload);
    expect(hb).not.toBeNull();
    expect(hb!.ts).toBe(payload.ts);
    expect(hb!.phase).toBe("cycle_complete");
    expect(hb!.datasource).toBe("polymarket_ws");
    expect(hb!.live_trading).toBe(false);
    expect(hb!.detected).toBe(12);
    expect(hb!.copied).toBe(3);
    expect(hb!.skipped).toBe(9);
    expect(hb!.placed).toBe(3);
    expect(hb!.followed_leaders).toEqual([
      "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]);
  });

  it("also parses the {value:...} wrapped shape (writer tolerance)", () => {
    const payload = pythonHeartbeatPayload(isoWithOffset(new Date(), true));
    const hb = coerceHeartbeat({ value: payload });
    expect(hb).not.toBeNull();
    expect(hb!.phase).toBe("cycle_complete");
  });

  it("accepts Python isoformat WITH microseconds (.ffffff+00:00)", () => {
    const tsMicros = isoWithOffset(new Date(), true);
    const hb = coerceHeartbeat(pythonHeartbeatPayload(tsMicros));
    expect(hb).not.toBeNull();
    // The micros timestamp must be a valid parseable Date.
    expect(Number.isNaN(new Date(hb!.ts).getTime())).toBe(false);
  });

  it("a FRESH Python heartbeat -> liveness 'running'", () => {
    const fresh = pythonHeartbeatPayload(isoWithOffset(new Date(), false));
    const hb = coerceHeartbeat(fresh);
    expect(heartbeatLiveness(hb)).toBe("running");
  });

  it("a Python heartbeat older than the stale window -> 'stale'", () => {
    // (HEARTBEAT_STALE_MINUTES + 1) minutes ago -> beyond the threshold.
    const old = new Date(
      Date.now() - (HEARTBEAT_STALE_MINUTES + 1) * 60_000
    );
    const stale = pythonHeartbeatPayload(isoWithOffset(old, true));
    const hb = coerceHeartbeat(stale);
    expect(hb).not.toBeNull();
    expect(heartbeatLiveness(hb)).toBe("stale");
  });

  it("a heartbeat just inside the stale window is still 'running' (generous, no flap)", () => {
    // `heartbeatLiveness` uses `ageMs > staleMinutes*60000` (strictly
    // greater), so a heartbeat slightly YOUNGER than the threshold must
    // stay 'running'. Pin `now` explicitly to make the boundary exact and
    // not subject to wall-clock drift between construction and evaluation.
    const now = new Date("2026-05-18T12:00:00.000Z");
    const justInside = new Date(
      now.getTime() - (HEARTBEAT_STALE_MINUTES * 60_000 - 1_000)
    );
    const hb = coerceHeartbeat(
      pythonHeartbeatPayload(isoWithOffset(justInside, false))
    );
    expect(heartbeatLiveness(hb, now)).toBe("running");

    // And EXACTLY at the threshold is also 'running' (strict `>` boundary).
    const exact = new Date(now.getTime() - HEARTBEAT_STALE_MINUTES * 60_000);
    const hbExact = coerceHeartbeat(
      pythonHeartbeatPayload(isoWithOffset(exact, false))
    );
    expect(heartbeatLiveness(hbExact, now)).toBe("running");
  });

  it("an absent/garbage row -> null -> liveness 'missing' (fail-safe DOWN)", () => {
    expect(coerceHeartbeat(null)).toBeNull();
    expect(coerceHeartbeat(undefined)).toBeNull();
    expect(coerceHeartbeat({})).toBeNull(); // no `ts`
    expect(coerceHeartbeat({ ts: "not-a-date" })).toBeNull();
    expect(heartbeatLiveness(coerceHeartbeat(null))).toBe("missing");
  });

  it("a partial Python payload (only the load-bearing `ts`) still works", () => {
    // The TS side documents every non-ts field as optional/defensive — a
    // minimal producer (or a future Python field rename) must degrade to a
    // valid liveness verdict, never crash.
    const minimal = { ts: isoWithOffset(new Date(), false) };
    const hb = coerceHeartbeat(minimal);
    expect(hb).not.toBeNull();
    expect(hb!.phase).toBeUndefined();
    expect(heartbeatLiveness(hb)).toBe("running");
  });
});
