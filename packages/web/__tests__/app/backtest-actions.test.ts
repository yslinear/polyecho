import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * createBacktestJob server action: verify scenario -> slippage/delay
 * resolution, the inserted row shape, and the post-insert redirect.
 * Drizzle (`@/lib/db`) and next/navigation are fully mocked: no DB, no nav.
 */

const returning = vi.fn().mockResolvedValue([{ id: "new-job-id" }]);
const values = vi.fn(() => ({ returning }));
const insert = vi.fn(() => ({ values }));

vi.mock("@/lib/db", () => ({ db: { insert: (...a: unknown[]) => insert(...a) } }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const redirect = vi.fn((url: string) => {
  // Next's real redirect throws NEXT_REDIRECT; emulate so callers can catch.
  throw new Error(`NEXT_REDIRECT;${url}`);
});
vi.mock("next/navigation", () => ({
  redirect: (url: string) => redirect(url),
}));

import { createBacktestJob } from "@/app/backtest/actions";
import type { BacktestFormValues } from "@/app/backtest/actions";

const base: BacktestFormValues = {
  leader_address: "0xabc",
  start_date: "2025-01-01",
  end_date: "2025-04-01",
  initial_capital_usd: 1000,
  copy_ratio: 0.001,
  fixed_size_usd: undefined,
  max_position_usd: 50,
  min_trade_size_usd: 1,
  daily_loss_limit_usd: 100,
  slippage_bps: 150,
  execution_delay_seconds: 30,
  category_whitelist: ["Politics"],
  scenario: "triple",
};

describe("createBacktestJob", () => {
  beforeEach(() => {
    insert.mockClear();
    values.mockClear();
    returning.mockClear().mockResolvedValue([{ id: "new-job-id" }]);
    redirect.mockClear();
  });

  it("inserts a pending job and redirects to /backtest/<id>", async () => {
    await expect(createBacktestJob(base)).rejects.toThrow(
      "NEXT_REDIRECT;/backtest/new-job-id"
    );
    expect(insert).toHaveBeenCalledTimes(1);
    const row = values.mock.calls[0][0] as Record<string, unknown>;
    expect(row.leader_addresses).toEqual(["0xabc"]);
    expect(row.status).toBe("pending");
    expect(row.progress).toBe("0");
    expect(row.initial_capital_usd).toBe("1000"); // stringified for numeric col
    expect(redirect).toHaveBeenCalledWith("/backtest/new-job-id");
  });

  it("overrides slippage/delay for the optimistic scenario", async () => {
    await expect(
      createBacktestJob({ ...base, scenario: "optimistic" })
    ).rejects.toThrow(/NEXT_REDIRECT/);
    const row = values.mock.calls[0][0] as Record<string, unknown>;
    expect(row.slippage_bps).toBe(50);
    expect(row.execution_delay_seconds).toBe(10);
  });

  it("overrides slippage/delay for the pessimistic scenario", async () => {
    await expect(
      createBacktestJob({ ...base, scenario: "pessimistic" })
    ).rejects.toThrow(/NEXT_REDIRECT/);
    const row = values.mock.calls[0][0] as Record<string, unknown>;
    expect(row.slippage_bps).toBe(300);
    expect(row.execution_delay_seconds).toBe(60);
  });

  it("keeps user-provided slippage/delay for the 'triple' baseline", async () => {
    await expect(
      createBacktestJob({
        ...base,
        scenario: "triple",
        slippage_bps: 222,
        execution_delay_seconds: 44,
      })
    ).rejects.toThrow(/NEXT_REDIRECT/);
    const row = values.mock.calls[0][0] as Record<string, unknown>;
    expect(row.slippage_bps).toBe(222);
    expect(row.execution_delay_seconds).toBe(44);
  });

  it("nulls copy_ratio / fixed_size_usd in template_params when undefined", async () => {
    await expect(
      createBacktestJob({
        ...base,
        copy_ratio: undefined,
        fixed_size_usd: 25,
      })
    ).rejects.toThrow(/NEXT_REDIRECT/);
    const row = values.mock.calls[0][0] as {
      template_params: Record<string, unknown>;
    };
    expect(row.template_params.copy_ratio).toBeNull();
    expect(row.template_params.fixed_size_usd).toBe(25);
    expect(row.template_params.category_whitelist).toEqual(["Politics"]);
  });

  // ---- rev-web I-3: server-side guards before insert ----------------------

  it("rejects a malformed start_date and never inserts", async () => {
    await expect(
      createBacktestJob({ ...base, start_date: "2025/01/01" })
    ).rejects.toThrow(/起始日期格式錯誤/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects a malformed end_date", async () => {
    await expect(
      createBacktestJob({ ...base, end_date: "not-a-date" })
    ).rejects.toThrow(/結束日期格式錯誤/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects non-positive initial_capital_usd", async () => {
    await expect(
      createBacktestJob({ ...base, initial_capital_usd: 0 })
    ).rejects.toThrow(/初始資金必須大於 0/);
    await expect(
      createBacktestJob({ ...base, initial_capital_usd: -100 })
    ).rejects.toThrow(/初始資金必須大於 0/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects negative slippage_bps", async () => {
    await expect(
      createBacktestJob({ ...base, slippage_bps: -1, scenario: "triple" })
    ).rejects.toThrow(/滑價/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("rejects negative execution_delay_seconds", async () => {
    await expect(
      createBacktestJob({
        ...base,
        execution_delay_seconds: -5,
        scenario: "triple",
      })
    ).rejects.toThrow(/執行延遲/);
    expect(insert).not.toHaveBeenCalled();
  });

  it("still accepts a fully valid job (no behaviour change)", async () => {
    await expect(createBacktestJob(base)).rejects.toThrow(/NEXT_REDIRECT/);
    expect(insert).toHaveBeenCalledTimes(1);
  });
});
