import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

/**
 * <TraderHeartbeatStatus /> + the settings-keys coercion/liveness it
 * depends on — PRD §7.4 / P1-5 bot liveness, READ-ONLY.
 *
 *  - recent heartbeat -> "運行中" + relative "最後心跳"
 *  - stale heartbeat  -> "停止 / 無心跳（> N 分鐘）"
 *  - missing row      -> "無心跳"
 *  - renders NO control (no button/switch/checkbox)
 */

import { TraderHeartbeatStatus } from "@/components/trader-heartbeat-status";
import {
  coerceHeartbeat,
  heartbeatLiveness,
  HEARTBEAT_STALE_MINUTES,
} from "@/lib/settings-keys";
import { t } from "@/lib/i18n";

// Expected labels are derived from the i18n dictionary, not hardcoded:
// the test asserts the liveness→label mapping + data-attr + no-control
// invariant (the load-bearing behaviour), robust to translation. The
// {{time}}/{{minutes}} placeholders are stripped so the assertion does
// not depend on the runtime-formatted relative time or the threshold.
const PLACEHOLDER = /\s*\{\{[^}]+\}\}\s*/g;
const RUNNING_LABEL = t("common.heartbeat.running", { time: "" }).replace(
  PLACEHOLDER,
  ""
);
const STALE_LABEL = t("common.heartbeat.stale", {
  minutes: HEARTBEAT_STALE_MINUTES,
});
const MISSING_LABEL = t("common.heartbeat.missing");

const NOW = new Date("2026-05-18T12:00:00.000Z");

function payload(tsOffsetSeconds: number) {
  return {
    ts: new Date(NOW.getTime() - tsOffsetSeconds * 1000).toISOString(),
    phase: "supervised",
    datasource: "live",
    live_trading: false,
    detected: 3,
    copied: 1,
    skipped: 2,
    placed: 1,
    followed_leaders: ["0xabc"],
  };
}

describe("coerceHeartbeat", () => {
  it("reads a bare object payload", () => {
    const hb = coerceHeartbeat(payload(10));
    expect(hb?.ts).toBeTypeOf("string");
    expect(hb?.detected).toBe(3);
  });

  it("reads a {value:{...}} wrapped payload (jsonb tolerance)", () => {
    const hb = coerceHeartbeat({ value: payload(10) });
    expect(hb?.phase).toBe("supervised");
  });

  it("returns null for missing / garbage rows", () => {
    expect(coerceHeartbeat(null)).toBeNull();
    expect(coerceHeartbeat(undefined)).toBeNull();
    expect(coerceHeartbeat({ ts: "not-a-date" })).toBeNull();
    expect(coerceHeartbeat({ nope: 1 })).toBeNull();
  });
});

describe("heartbeatLiveness", () => {
  it("running when the heartbeat is recent", () => {
    expect(heartbeatLiveness(coerceHeartbeat(payload(30)), NOW)).toBe(
      "running"
    );
  });

  it("stale when older than the threshold", () => {
    const old = coerceHeartbeat(
      payload(HEARTBEAT_STALE_MINUTES * 60 + 60)
    );
    expect(heartbeatLiveness(old, NOW)).toBe("stale");
  });

  it("missing when there is no heartbeat", () => {
    expect(heartbeatLiveness(null, NOW)).toBe("missing");
  });
});

describe("TraderHeartbeatStatus", () => {
  it("renders the running label + relative time with a recent heartbeat", () => {
    const hb = coerceHeartbeat(payload(45));
    render(
      <TraderHeartbeatStatus
        heartbeat={hb}
        liveness={heartbeatLiveness(hb, NOW)}
      />
    );
    const el = screen.getByRole("status");
    // Static part of the running label (placeholder stripped) is present…
    expect(el.textContent).toContain(RUNNING_LABEL);
    // …and the {{time}} slot is actually filled by a relative-time string.
    expect(el.textContent!.length).toBeGreaterThan(RUNNING_LABEL.length);
    expect(el).toHaveAttribute("data-heartbeat-status", "running");
  });

  it("renders the stale label when the heartbeat is stale", () => {
    const hb = coerceHeartbeat(payload(HEARTBEAT_STALE_MINUTES * 60 + 120));
    render(
      <TraderHeartbeatStatus
        heartbeat={hb}
        liveness={heartbeatLiveness(hb, NOW)}
      />
    );
    const el = screen.getByRole("status");
    expect(el.textContent).toBe(STALE_LABEL);
    expect(el).toHaveAttribute("data-heartbeat-status", "stale");
  });

  it("renders the missing label when the row is missing", () => {
    render(
      <TraderHeartbeatStatus heartbeat={null} liveness="missing" />
    );
    const el = screen.getByRole("status");
    expect(el.textContent).toBe(MISSING_LABEL);
    expect(el).toHaveAttribute("data-heartbeat-status", "missing");
  });

  it("renders NO control (no button, switch or checkbox)", () => {
    const { container } = render(
      <TraderHeartbeatStatus heartbeat={null} liveness="missing" />
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(container.querySelector("button")).toBeNull();
    expect(container.querySelector("input")).toBeNull();
  });
});
