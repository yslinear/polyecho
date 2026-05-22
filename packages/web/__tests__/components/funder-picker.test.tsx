import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * FunderPicker (P3) — behaviour contract for the /accounts import-form
 * client island that derives 4 candidate funder addresses for the typed
 * EOA and lets the operator pick by USDC balance.
 *
 * Mocks the two Server Actions it depends on:
 *   - `deriveAllFunders(eoa)` (proxies to internal detect container)
 *   - `probeTotalBalance(addr)` (per-candidate USDC balance probe)
 *
 * EXPLICIT TRIGGER: detection no longer auto-fires on a valid EOA. The
 * picker first shows a 「查詢錢包餘額」 button; detection runs only on
 * that click. `renderTriggered` below renders + clicks it so the
 * post-trigger assertions are unchanged.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const deriveAllFunders = vi.fn();
const probeTotalBalance = vi.fn();

vi.mock("@/app/accounts/actions", () => ({
  deriveAllFunders: (...a: unknown[]) => deriveAllFunders(...a),
}));

vi.mock("@/app/accounts/balance", () => ({
  probeTotalBalance: (...a: unknown[]) => probeTotalBalance(...a),
}));

import { FunderPicker } from "@/app/accounts/funder-picker";

const VALID_EOA = "0x1111111111111111111111111111111111111111";
const ADDR_0 = "0x1111111111111111111111111111111111111111";
const ADDR_1 = "0x2222222222222222222222222222222222222222";
const ADDR_2 = "0x3333333333333333333333333333333333333333";
const ADDR_3 = "0x4444444444444444444444444444444444444444";

/** Ceiling for any waitFor that needs to outlast the async detect/probe. */
const SETTLE = { timeout: 3000 };

function mkDetectOk(opts?: { addr2Null?: boolean }) {
  return {
    ok: true as const,
    candidates: [
      { sigType: 0, mode: "eoa", address: ADDR_0, derived: false },
      { sigType: 1, mode: "magic", address: ADDR_1, derived: true },
      {
        sigType: 2,
        mode: "safe",
        address: opts?.addr2Null ? null : ADDR_2,
        derived: false,
        ...(opts?.addr2Null ? { error: "no safe" } : {}),
      },
      { sigType: 3, mode: "poly_1271", address: ADDR_3, derived: true },
    ],
  };
}

function mkBalance(positions: number, idle: number) {
  return {
    ok: true,
    positionsUsd: positions,
    idleUsdc: idle,
    totalUsd: positions + idle,
    message: "",
  };
}

/**
 * Render the picker and click the explicit 「查詢錢包餘額」 lookup button
 * so detection fires. Detection is gated behind this button — it is NOT
 * auto-fired on a valid EOA.
 */
async function renderTriggered(eoa: string, onPick: () => void = vi.fn()) {
  const user = userEvent.setup();
  const utils = render(<FunderPicker eoa={eoa} onPick={onPick} />);
  await user.click(screen.getByRole("button", { name: /查詢錢包餘額/ }));
  return { ...utils, user, onPick };
}

beforeEach(() => {
  deriveAllFunders.mockReset();
  probeTotalBalance.mockReset();
});

describe("FunderPicker — visibility gating", () => {
  it("renders nothing when EOA is empty", () => {
    const { container } = render(<FunderPicker eoa="" onPick={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(deriveAllFunders).not.toHaveBeenCalled();
  });

  it("renders nothing when EOA fails EVM_ADDRESS_RE", () => {
    const { container } = render(
      <FunderPicker eoa="0xnot-a-real-address" onPick={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(deriveAllFunders).not.toHaveBeenCalled();
  });

  it("a valid EOA shows the lookup button but does NOT call deriveAllFunders until it is clicked", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance.mockResolvedValue(mkBalance(0, 0));

    const user = userEvent.setup();
    render(<FunderPicker eoa={VALID_EOA} onPick={vi.fn()} />);

    // Button present, no rows, no Server Action call yet.
    const lookup = screen.getByRole("button", { name: /查詢錢包餘額/ });
    expect(lookup).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(deriveAllFunders).not.toHaveBeenCalled();

    // Only the explicit click triggers detection.
    await user.click(lookup);
    await waitFor(() => {
      expect(deriveAllFunders).toHaveBeenCalledWith(VALID_EOA);
    }, SETTLE);
  });
});

describe("FunderPicker — probing skeletons", () => {
  it("shows skeleton rows in probing state and disables each row button while probing", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    // Never-resolves promise — keeps all 4 rows pinned in 'probing'.
    probeTotalBalance.mockReturnValue(new Promise(() => {}));

    await renderTriggered(VALID_EOA);

    // Wait until rows are seeded (4 actual button rows), past the initial
    // skeleton-only loading state which also renders 4 <li>s.
    await waitFor(() => {
      const btns = screen.getAllByRole("button");
      expect(btns.length).toBe(4);
    }, SETTLE);

    // Every row's button is disabled (still probing).
    for (const b of screen.getAllByRole("button")) {
      expect(b).toBeDisabled();
    }
    // And each row contains a Skeleton placeholder (animate-pulse div).
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });
});

describe("FunderPicker — balance rendering", () => {
  it("renders text-gain class on row when positions > 0", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance
      .mockResolvedValueOnce(mkBalance(123.45, 0))
      .mockResolvedValueOnce(mkBalance(0, 0))
      .mockResolvedValueOnce(mkBalance(0, 0))
      .mockResolvedValueOnce(mkBalance(0, 0));

    await renderTriggered(VALID_EOA);

    await waitFor(() => {
      expect(screen.getAllByText(/\$123\.45/).length).toBeGreaterThan(0);
    }, SETTLE);

    expect(document.querySelectorAll(".text-gain").length).toBeGreaterThan(0);
  });

  it("renders text-gain when only idle > 0 (positions zero)", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance
      .mockResolvedValueOnce(mkBalance(0, 0))
      .mockResolvedValueOnce(mkBalance(0, 42.0))
      .mockResolvedValueOnce(mkBalance(0, 0))
      .mockResolvedValueOnce(mkBalance(0, 0));

    await renderTriggered(VALID_EOA);

    await waitFor(() => {
      expect(screen.getAllByText(/\$42\.00/).length).toBeGreaterThan(0);
    }, SETTLE);
    expect(document.querySelectorAll(".text-gain").length).toBeGreaterThan(0);
  });
});

describe("FunderPicker — onPick + collapsed state", () => {
  it("clicking a row fires onPick with the correct candidate (sigType + address)", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance.mockResolvedValue(mkBalance(10, 5));

    const onPick = vi.fn();
    const { user } = await renderTriggered(VALID_EOA, onPick);

    await waitFor(() => {
      const btns = screen.getAllByRole("button");
      expect(btns.length).toBe(4);
      expect(btns.every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
    }, SETTLE);

    const items = screen.getAllByRole("listitem");
    const sig1Btn = within(items[1]).getByRole("button");
    await user.click(sig1Btn);

    expect(onPick).toHaveBeenCalledTimes(1);
    const got = onPick.mock.calls[0][0];
    expect(got.sigType).toBe(1);
    expect(got.address).toBe(ADDR_1);
  });

  it("button is disabled (cannot be clicked) when candidate has address: null", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk({ addr2Null: true }));
    probeTotalBalance.mockResolvedValue(mkBalance(0, 0));

    const onPick = vi.fn();
    const { user } = await renderTriggered(VALID_EOA, onPick);

    await waitFor(() => {
      const items = screen.getAllByRole("listitem");
      expect(items.length).toBe(4);
      // sig2 row (index 2) has null address — its button stays disabled.
      const sig2Btn = within(items[2]).getByRole("button");
      expect(sig2Btn).toBeDisabled();
    }, SETTLE);

    const items = screen.getAllByRole("listitem");
    const sig2Btn = within(items[2]).getByRole("button");
    await user.click(sig2Btn);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("collapses to a selected line + reselect after pick; reselect re-opens the picker", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance.mockResolvedValue(mkBalance(10, 5));

    const { user } = await renderTriggered(VALID_EOA);

    await waitFor(() => {
      const btns = screen.getAllByRole("button");
      expect(btns.length).toBe(4);
      expect(btns.every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
    }, SETTLE);

    const items = screen.getAllByRole("listitem");
    await user.click(within(items[0]).getByRole("button"));

    await waitFor(() => {
      expect(screen.queryByRole("list")).not.toBeInTheDocument();
    });
    expect(screen.getByText(/已選/)).toBeInTheDocument();
    const reselect = screen.getByRole("button", { name: /重選/ });
    expect(reselect).toBeInTheDocument();

    await user.click(reselect);
    await waitFor(() => {
      expect(screen.getByRole("list")).toBeInTheDocument();
    });
    expect(screen.getAllByRole("listitem").length).toBe(4);
  });
});

describe("FunderPicker — service unavailable", () => {
  it("when deriveAllFunders returns {ok:false}, shows the unavailable note and no rows", async () => {
    deriveAllFunders.mockResolvedValue({
      ok: false,
      reason: "偵測服務無法觸及",
    });

    await renderTriggered(VALID_EOA);

    await waitFor(() => {
      expect(screen.getByText(/自動偵測暫不可用/)).toBeInTheDocument();
    }, SETTLE);
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(probeTotalBalance).not.toHaveBeenCalled();
  });
});

describe("FunderPicker — all-zero empty state", () => {
  it("when all 4 balances probe to 0/0, the allZeroNote renders", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance.mockResolvedValue(mkBalance(0, 0));

    await renderTriggered(VALID_EOA);

    await waitFor(() => {
      expect(screen.getByText(/還沒入金到任何錢包/)).toBeInTheDocument();
    }, SETTLE);
  });
});

describe("FunderPicker — keyboard + a11y", () => {
  it("Tab focuses each row button in order; Enter on a focused row fires onPick", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance.mockResolvedValue(mkBalance(10, 5));

    const onPick = vi.fn();
    const { user } = await renderTriggered(VALID_EOA, onPick);

    await waitFor(() => {
      const btns = screen.getAllByRole("button");
      expect(btns.length).toBe(4);
      expect(btns.every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
    }, SETTLE);

    const items = screen.getAllByRole("listitem");
    const buttons = items.map((li) => within(li).getByRole("button"));

    await user.tab();
    expect(buttons[0]).toHaveFocus();
    await user.tab();
    expect(buttons[1]).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick.mock.calls[0][0].sigType).toBe(1);
  });

  it("each row button has an aria-label including the mode name", async () => {
    deriveAllFunders.mockResolvedValue(mkDetectOk());
    probeTotalBalance.mockResolvedValue(mkBalance(10, 5));

    await renderTriggered(VALID_EOA);

    // Wait for the 4 row BUTTONS (not just listitems — the skeleton
    // loading state also has 4 <li>s without buttons).
    await waitFor(() => {
      expect(screen.getAllByRole("button").length).toBe(4);
    }, SETTLE);

    const items = screen.getAllByRole("listitem");
    for (const li of items) {
      const btn = within(li).getByRole("button");
      const label = btn.getAttribute("aria-label") ?? "";
      expect(label.length).toBeGreaterThan(0);
      expect(label).toMatch(/EOA|Magic Proxy|Gnosis Safe|Deposit Wallet/);
    }
  });
});
