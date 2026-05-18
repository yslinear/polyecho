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
  it("renders 運行中 with a recent heartbeat", () => {
    const hb = coerceHeartbeat(payload(45));
    render(
      <TraderHeartbeatStatus
        heartbeat={hb}
        liveness={heartbeatLiveness(hb, NOW)}
      />
    );
    const el = screen.getByRole("status");
    expect(el.textContent).toContain("運行中");
    expect(el.textContent).toContain("最後心跳");
    expect(el).toHaveAttribute("data-heartbeat-status", "running");
  });

  it("renders 停止 / 無心跳 when the heartbeat is stale", () => {
    const hb = coerceHeartbeat(payload(HEARTBEAT_STALE_MINUTES * 60 + 120));
    render(
      <TraderHeartbeatStatus
        heartbeat={hb}
        liveness={heartbeatLiveness(hb, NOW)}
      />
    );
    const el = screen.getByRole("status");
    expect(el.textContent).toContain("停止");
    expect(el.textContent).toContain("無心跳");
    expect(el).toHaveAttribute("data-heartbeat-status", "stale");
  });

  it("renders 無心跳 when the row is missing", () => {
    render(
      <TraderHeartbeatStatus heartbeat={null} liveness="missing" />
    );
    const el = screen.getByRole("status");
    expect(el.textContent).toContain("無心跳");
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
