import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Mock the server action so no DB / network is touched.
const createBacktestJob = vi.fn();
vi.mock("@/app/backtest/actions", () => ({
  createBacktestJob: (...args: unknown[]) => createBacktestJob(...args),
}));

// sonner toast -> spies
const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { BacktestForm } from "@/app/backtest/new/backtest-form";

describe("BacktestForm (zod validation + server action)", () => {
  beforeEach(() => {
    createBacktestJob.mockReset().mockResolvedValue({ id: "job-1" });
    toastSuccess.mockReset();
    toastError.mockReset();
  });

  it("renders the key fields and the submit button", () => {
    render(<BacktestForm />);
    expect(screen.getByLabelText("Leader Address")).toBeInTheDocument();
    expect(screen.getByLabelText("Start Date")).toBeInTheDocument();
    expect(screen.getByLabelText("End Date")).toBeInTheDocument();
    expect(screen.getByLabelText("Initial Capital (USD)")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Queue Backtest" })
    ).toBeInTheDocument();
  });

  it("prefills the leader address from the defaultLeader prop", () => {
    render(<BacktestForm defaultLeader="0xdeadbeef" />);
    expect(screen.getByLabelText("Leader Address")).toHaveValue("0xdeadbeef");
  });

  it("blocks submit and shows the min-length error for a too-short wallet", async () => {
    render(<BacktestForm />);
    const addr = screen.getByLabelText("Leader Address");
    await userEvent.clear(addr);
    await userEvent.type(addr, "0xabc"); // < 10 chars -> min() message

    await userEvent.click(
      screen.getByRole("button", { name: "Queue Backtest" })
    );

    expect(
      await screen.findByText("Enter a valid wallet address")
    ).toBeInTheDocument();
    expect(createBacktestJob).not.toHaveBeenCalled();
  });

  it("enforces the 0x-hex regex on the wallet address", async () => {
    render(<BacktestForm />);
    const addr = screen.getByLabelText("Leader Address");
    await userEvent.clear(addr);
    await userEvent.type(addr, "1234567890abcdef"); // long enough, no 0x

    await userEvent.click(
      screen.getByRole("button", { name: "Queue Backtest" })
    );
    expect(await screen.findByText("Must start with 0x")).toBeInTheDocument();
    expect(createBacktestJob).not.toHaveBeenCalled();
  });

  it("rejects when start_date is not before end_date", async () => {
    render(<BacktestForm />);
    await userEvent.clear(screen.getByLabelText("Leader Address"));
    await userEvent.type(
      screen.getByLabelText("Leader Address"),
      "0xabcabcabcabc"
    );
    // Date inputs: set start after end
    const start = screen.getByLabelText("Start Date");
    const end = screen.getByLabelText("End Date");
    await userEvent.clear(start);
    await userEvent.type(start, "2025-12-31");
    await userEvent.clear(end);
    await userEvent.type(end, "2025-01-01");

    await userEvent.click(
      screen.getByRole("button", { name: "Queue Backtest" })
    );
    expect(
      await screen.findByText("start_date must be before end_date")
    ).toBeInTheDocument();
    expect(createBacktestJob).not.toHaveBeenCalled();
  });

  it("submits valid data: calls the action with parsed payload and toasts success", async () => {
    render(<BacktestForm />);
    const addr = screen.getByLabelText("Leader Address");
    await userEvent.clear(addr);
    await userEvent.type(
      addr,
      "0x1111111111111111111111111111111111111111"
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Queue Backtest" })
    );

    await waitFor(() => expect(createBacktestJob).toHaveBeenCalledTimes(1));
    const payload = createBacktestJob.mock.calls[0][0];
    expect(payload.leader_address).toBe(
      "0x1111111111111111111111111111111111111111"
    );
    // copy_ratio default 0.001 is numeric-coerced; fixed_size omitted
    expect(payload.copy_ratio).toBe(0.001);
    expect(payload.fixed_size_usd).toBeUndefined();
    expect(payload.scenario).toBe("triple");
    expect(Array.isArray(payload.category_whitelist)).toBe(true);
    expect(payload.initial_capital_usd).toBe(1000);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
  });

  it("splits the comma-separated category whitelist into a trimmed array", async () => {
    render(<BacktestForm />);
    await userEvent.clear(screen.getByLabelText("Leader Address"));
    await userEvent.type(
      screen.getByLabelText("Leader Address"),
      "0x2222222222222222222222222222222222222222"
    );
    const cw = screen.getByLabelText("Category Whitelist");
    await userEvent.type(cw, "Politics, Sports ,  Economy ");

    await userEvent.click(
      screen.getByRole("button", { name: "Queue Backtest" })
    );
    await waitFor(() => expect(createBacktestJob).toHaveBeenCalled());
    expect(createBacktestJob.mock.calls[0][0].category_whitelist).toEqual([
      "Politics",
      "Sports",
      "Economy",
    ]);
  });

  it("surfaces a server-action failure via an error toast (no rethrow for non-redirect)", async () => {
    createBacktestJob.mockRejectedValueOnce(new Error("db down"));
    render(<BacktestForm />);
    await userEvent.clear(screen.getByLabelText("Leader Address"));
    await userEvent.type(
      screen.getByLabelText("Leader Address"),
      "0x3333333333333333333333333333333333333333"
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Queue Backtest" })
    );
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls[0][0]).toBe("Failed to queue backtest");
  });
});
