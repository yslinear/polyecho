import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * GlobalPauseToggle island (PRD §5.8 top-bar 全域暫停/恢復).
 * Reflects the layout-supplied `paused` state and calls setGlobalPaused on
 * click, then toasts + router.refresh(). DB-free (action mocked).
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const refresh = vi.fn<() => void>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const setGlobalPaused = vi.fn(async (paused: boolean) => ({
  ok: true,
  message: paused ? "已全域暫停：trader-core 停止所有跟單。" : "已恢復。",
  paused,
}));
vi.mock("@/app/settings/actions", () => ({
  setGlobalPaused: (...a: unknown[]) => setGlobalPaused(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { GlobalPauseToggle } from "@/components/global-pause-toggle";

beforeEach(() => {
  refresh.mockClear();
  setGlobalPaused.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("GlobalPauseToggle", () => {
  it("shows '暫停全部' when running and pauses on click", async () => {
    render(<GlobalPauseToggle paused={false} />);
    const btn = screen.getByRole("button", { name: "暫停全部跟單" });
    expect(btn).toHaveTextContent("暫停全部");
    await userEvent.click(btn);
    expect(setGlobalPaused).toHaveBeenCalledWith(true);
    expect(toastSuccess).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("shows '已暫停' when paused and resumes on click", async () => {
    render(<GlobalPauseToggle paused={true} />);
    const btn = screen.getByRole("button", { name: "恢復全部跟單" });
    expect(btn).toHaveTextContent("已暫停");
    expect(btn).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(btn);
    expect(setGlobalPaused).toHaveBeenCalledWith(false);
  });

  it("toasts an error when the action throws", async () => {
    setGlobalPaused.mockRejectedValueOnce(new Error("db down"));
    render(<GlobalPauseToggle paused={false} />);
    await userEvent.click(
      screen.getByRole("button", { name: "暫停全部跟單" })
    );
    expect(toastError).toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});
