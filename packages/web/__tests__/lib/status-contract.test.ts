import { describe, it, expect } from "vitest";

import {
  WORKER_HEARTBEAT_KEYS,
  TRADER_HEARTBEAT_KEY,
  DISCOVERY_HEARTBEAT_KEY,
  BACKTEST_HEARTBEAT_KEY,
  PUSH_HEARTBEAT_KEY,
  INTEGRATION_STATUS_KEY,
  coerceHeartbeat,
  heartbeatLiveness,
  coerceIntegrationStatus,
  probeVerdict,
  HEARTBEAT_STALE_MINUTES,
} from "@/lib/settings-keys";
import {
  workerPresentation,
  probePresentation,
  integrationNoData,
} from "@/lib/status";
import { t } from "@/lib/i18n";

// `lib/status.tsx` renders verdict labels through the i18n dictionary.
// Expected labels are derived from the SAME `status.verdict.*` keys the
// presentation map uses, so this CROSS-STACK contract still pins the
// running/stale/missing + ok/down behaviour while being robust to
// translation (the variant/token/icon channels are the colour-blind-safe
// invariants and remain asserted verbatim).
const L_RUNNING = t("status.verdict.running");
const L_STALE = t("status.verdict.stale", {
  minutes: HEARTBEAT_STALE_MINUTES,
});
const L_NO_DATA = t("status.verdict.noData");
const L_OK = t("status.verdict.ok");
const L_DOWN = t("status.verdict.down");

/**
 * CROSS-STACK contract guard for the /status `settings` rows — the sibling
 * of `heartbeat-contract.test.ts`. The per-worker heartbeats and the
 * `integration_status` payload are DUPLICATED across two languages with no
 * shared codegen: the Python producers and this TS consumer must agree
 * byte-for-byte or /status silently lies. This test pins the EXACT JSON the
 * Python side writes (incl. the legacy `{value:..}` fold, an `ok:false`
 * probe, and missing / stale / null cases) and asserts the coercers +
 * liveness + presentation render running / stale / missing and ok / down
 * correctly.
 *
 * Authoritative producers (read-only reference, do NOT import — different
 * runtime):
 *
 *   polyecho.shared.heartbeat.write_heartbeat
 *     upserts settings(key=<worker>_heartbeat, value=info), `info` BARE:
 *       { "ts": datetime.now(tz=UTC).isoformat(), "phase": str,
 *         "datasource": str, "live_trading": bool,
 *         "detected"/"copied"/"skipped"/"placed": int, ... }
 *
 *   polyecho.shared.heartbeat.write_integration_status
 *     upserts settings(key="integration_status", value=info), `info` BARE:
 *       { "checked_at": <iso>,
 *         "probes": { <name>: { "ok": bool, "detail": str,
 *                                "latency_ms": float|None,
 *                                "checked_at": <iso>|None }, ... } }
 *
 * `datetime.isoformat()` for a tz-aware UTC datetime renders e.g.
 * `2026-05-19T07:30:00+00:00` (or with microseconds). Both must parse via
 * `new Date()`.
 */

/** Python `datetime.now(tz=UTC).isoformat()` shapes (with/without micros). */
function isoWithOffset(d: Date, withMicros: boolean): string {
  const base = d.toISOString().replace("Z", "");
  const noMs = base.slice(0, 19);
  return withMicros ? `${noMs}.123456+00:00` : `${noMs}+00:00`;
}

/** The EXACT dict a Python worker `write_heartbeat` produces, given a ts. */
function pythonWorkerHeartbeat(tsIso: string): Record<string, unknown> {
  return {
    ts: tsIso,
    phase: "cycle_complete",
    datasource: "polymarket_ws",
    live_trading: false,
    detected: 7,
    copied: 2,
    skipped: 5,
    placed: 2,
    followed_leaders: ["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
  };
}

/** The EXACT dict Python `write_integration_status` produces. */
function pythonIntegrationStatus(checkedAtIso: string): Record<string, unknown> {
  return {
    checked_at: checkedAtIso,
    probes: {
      config: {
        ok: true,
        detail: "loaded 12 settings",
        latency_ms: null,
        checked_at: checkedAtIso,
      },
      database: {
        ok: true,
        detail: "SELECT 1 in 3ms",
        latency_ms: 3.4,
        checked_at: checkedAtIso,
      },
      polymarket_clob: {
        ok: true,
        detail: "GET /ok 200",
        latency_ms: 142.7,
        checked_at: checkedAtIso,
      },
      polymarket_data_api: {
        ok: true,
        detail: "GET /trades 200",
        latency_ms: 188.0,
        checked_at: checkedAtIso,
      },
      polygon_rpc: {
        ok: true,
        detail: "eth_blockNumber 0x abc",
        latency_ms: 96.2,
        checked_at: checkedAtIso,
      },
      // An honest DOWN probe — the load-bearing failure case.
      vpn_egress: {
        ok: false,
        detail: "egress IP not in allowed set (got 203.0.113.7)",
        latency_ms: null,
        checked_at: checkedAtIso,
      },
    },
  };
}

describe("cross-stack: worker heartbeat keys are the frozen contract", () => {
  it("uses the EXACT key strings the Python workers write", () => {
    expect(TRADER_HEARTBEAT_KEY).toBe("trader_heartbeat");
    expect(DISCOVERY_HEARTBEAT_KEY).toBe("discovery_heartbeat");
    expect(BACKTEST_HEARTBEAT_KEY).toBe("backtest_heartbeat");
    expect(PUSH_HEARTBEAT_KEY).toBe("push_heartbeat");
    expect(INTEGRATION_STATUS_KEY).toBe("integration_status");
    expect(WORKER_HEARTBEAT_KEYS).toEqual([
      "trader_heartbeat",
      "discovery_heartbeat",
      "backtest_heartbeat",
      "push_heartbeat",
    ]);
  });
});

describe("cross-stack: per-worker heartbeat -> running/stale/missing", () => {
  it("a FRESH bare Python worker heartbeat -> 'running' (Running badge)", () => {
    for (const key of WORKER_HEARTBEAT_KEYS) {
      const fresh = pythonWorkerHeartbeat(isoWithOffset(new Date(), false));
      const hb = coerceHeartbeat(fresh);
      expect(hb, key).not.toBeNull();
      const liveness = heartbeatLiveness(hb);
      expect(liveness, key).toBe("running");
      const p = workerPresentation(liveness);
      expect(p.token).toBe("running");
      expect(p.label).toBe(L_RUNNING);
      expect(p.variant).toBe("secondary");
    }
  });

  it("also parses the legacy {value:..} envelope (writer tolerance)", () => {
    const wrapped = { value: pythonWorkerHeartbeat(isoWithOffset(new Date(), true)) };
    const hb = coerceHeartbeat(wrapped);
    expect(hb).not.toBeNull();
    expect(hb!.phase).toBe("cycle_complete");
    expect(hb!.detected).toBe(7);
    expect(heartbeatLiveness(hb)).toBe("running");
  });

  it("a heartbeat older than the 5-min window -> 'stale' (Stale badge)", () => {
    const old = new Date(Date.now() - (HEARTBEAT_STALE_MINUTES + 1) * 60_000);
    const hb = coerceHeartbeat(pythonWorkerHeartbeat(isoWithOffset(old, true)));
    expect(hb).not.toBeNull();
    expect(heartbeatLiveness(hb)).toBe("stale");
    const p = workerPresentation("stale");
    expect(p.token).toBe("stale");
    expect(p.label).toBe(L_STALE);
    expect(p.variant).toBe("destructive");
  });

  it("an absent / garbage / null-ts row -> 'missing' (No data, NOT down)", () => {
    expect(coerceHeartbeat(undefined)).toBeNull();
    expect(coerceHeartbeat(null)).toBeNull();
    expect(coerceHeartbeat({})).toBeNull();
    expect(coerceHeartbeat({ ts: null })).toBeNull();
    expect(coerceHeartbeat({ ts: "not-a-date" })).toBeNull();
    const liveness = heartbeatLiveness(coerceHeartbeat(null));
    expect(liveness).toBe("missing");
    const p = workerPresentation(liveness);
    // PolyHermes "zero vs unavailable": missing is its OWN state, distinct
    // from a real failure — outline variant, not destructive.
    expect(p.token).toBe("missing");
    expect(p.label).toBe(L_NO_DATA);
    expect(p.variant).toBe("outline");
  });

  it("a partial payload (only the load-bearing ts) still degrades cleanly", () => {
    const minimal = { ts: isoWithOffset(new Date(), false) };
    const hb = coerceHeartbeat(minimal);
    expect(hb).not.toBeNull();
    expect(hb!.phase).toBeUndefined();
    expect(hb!.datasource).toBeUndefined();
    expect(heartbeatLiveness(hb)).toBe("running");
  });
});

describe("cross-stack: integration_status -> ok/down + checked_at", () => {
  it("parses the EXACT bare Python payload incl. an ok:false probe", () => {
    const at = isoWithOffset(new Date(), false);
    const status = coerceIntegrationStatus(pythonIntegrationStatus(at));

    expect(status.checked_at).toBe(at);
    expect(Object.keys(status.probes).sort()).toEqual([
      "config",
      "database",
      "polygon_rpc",
      "polymarket_clob",
      "polymarket_data_api",
      "vpn_egress",
    ]);

    const db = status.probes.database;
    expect(probeVerdict(db)).toBe("ok");
    expect(db.latency_ms).toBe(3.4);
    expect(db.detail).toBe("SELECT 1 in 3ms");
    const okP = probePresentation(probeVerdict(db));
    expect(okP.token).toBe("ok");
    expect(okP.label).toBe(L_OK);
    expect(okP.variant).toBe("secondary");

    // The load-bearing failure: ok:false must read as a REAL down, with a
    // distinct presentation from a worker's "No data".
    const vpn = status.probes.vpn_egress;
    expect(probeVerdict(vpn)).toBe("down");
    expect(vpn.latency_ms).toBeNull();
    const downP = probePresentation(probeVerdict(vpn));
    expect(downP.token).toBe("down");
    expect(downP.label).toBe(L_DOWN);
    expect(downP.variant).toBe("destructive");
  });

  it("also parses the legacy {value:..} envelope", () => {
    const at = isoWithOffset(new Date(), true);
    const status = coerceIntegrationStatus({
      value: pythonIntegrationStatus(at),
    });
    expect(status.checked_at).toBe(at);
    expect(probeVerdict(status.probes.config)).toBe("ok");
    expect(status.probes.config.latency_ms).toBeNull(); // Python None -> null
  });

  it("a Python `None` latency / null checked_at coerces to JS null", () => {
    const status = coerceIntegrationStatus({
      checked_at: null,
      probes: {
        polygon_rpc: {
          ok: false,
          detail: "timeout after 5s",
          latency_ms: null,
          checked_at: null,
        },
      },
    });
    expect(status.checked_at).toBeNull();
    const p = status.probes.polygon_rpc;
    expect(p.latency_ms).toBeNull();
    expect(p.checked_at).toBeNull();
    expect(probeVerdict(p)).toBe("down");
  });

  it("missing / garbage row -> {checked_at:null, probes:{}} (no throw)", () => {
    for (const bad of [
      null,
      undefined,
      42,
      "string",
      {},
      { probes: null },
      { probes: "nope" },
    ]) {
      const s = coerceIntegrationStatus(bad);
      expect(s.checked_at).toBeNull();
      expect(s.probes).toEqual({});
    }
    // No probes at all -> the page shows the honest "No data" badge.
    expect(integrationNoData().token).toBe("missing");
    expect(integrationNoData().label).toBe(L_NO_DATA);
    expect(integrationNoData().variant).toBe("outline");
  });

  it("a probe missing fields degrades, never throws (ok:false = fail-safe)", () => {
    const s = coerceIntegrationStatus({
      checked_at: "not-a-date", // unparseable -> null
      probes: {
        weird: {}, // no ok/detail/latency/checked_at
      },
    });
    expect(s.checked_at).toBeNull();
    const p = s.probes.weird;
    expect(p.ok).toBe(false); // anything not boolean-true = DOWN
    expect(p.detail).toBe("");
    expect(p.latency_ms).toBeNull();
    expect(p.checked_at).toBeNull();
    expect(probeVerdict(p)).toBe("down");
  });

  it("a rogue oversized detail is capped so it cannot blow out the row", () => {
    const huge = "x".repeat(5000);
    const s = coerceIntegrationStatus({
      checked_at: isoWithOffset(new Date(), false),
      probes: { config: { ok: true, detail: huge, latency_ms: 1, checked_at: null } },
    });
    expect(s.probes.config.detail.length).toBeLessThanOrEqual(241); // 240 + "…"
    expect(s.probes.config.detail.endsWith("…")).toBe(true);
  });
});
