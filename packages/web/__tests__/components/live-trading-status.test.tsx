import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

/**
 * <LiveTradingStatus /> — READ-ONLY indicator (PRD §5.7 high-danger tier).
 *
 *  - acked=false  -> LOCKED
 *  - acked=true   -> UNLOCKED
 *  - verbose copy names POLYECHO_LIVE_TRADING as the CLI-only binding switch
 *  - it renders NO enable/disable control (no button/switch/checkbox)
 */

import { LiveTradingStatus } from "@/components/live-trading-status";
import { t } from "@/lib/i18n";

// Expected text is derived from the i18n dictionary, not hardcoded: the
// test asserts the acked→LOCKED/UNLOCKED mapping and the no-control
// invariant (the load-bearing behaviour) and is robust to translation.
// LOCKED/UNLOCKED and POLYECHO_LIVE_TRADING are proper-noun tokens kept
// verbatim by the PROPER-NOUN POLICY.
const LOCKED = t("common.liveTrading.locked");
const UNLOCKED = t("common.liveTrading.unlocked");
const ariaFor = (state: string) =>
  t("common.liveTrading.pillLabel", { state });
const pillTextFor = (state: string) =>
  t("common.liveTrading.pillText", { state });

describe("LiveTradingStatus", () => {
  it("shows LOCKED when there is no live_trading_ack", () => {
    render(<LiveTradingStatus acked={false} />);
    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      ariaFor(LOCKED)
    );
    expect(
      screen.getByText(pillTextFor(LOCKED))
    ).toBeInTheDocument();
  });

  it("shows UNLOCKED when live_trading_ack exists", () => {
    render(<LiveTradingStatus acked={true} />);
    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      ariaFor(UNLOCKED)
    );
    expect(
      screen.getByText(pillTextFor(UNLOCKED))
    ).toBeInTheDocument();
  });

  it("verbose variant names POLYECHO_LIVE_TRADING as the CLI-only switch", () => {
    render(<LiveTradingStatus acked={false} variant="verbose" />);
    // POLYECHO_LIVE_TRADING is isolated in its own <code>, so it is a
    // standalone text node regardless of how the prose is translated.
    expect(screen.getByText("POLYECHO_LIVE_TRADING")).toBeInTheDocument();
    // The explainer wraps the <code>: assert the dictionary's lead-in and
    // tail-out fragments are rendered (proves the seam, robust to wording).
    expect(
      screen.getByText(t("common.liveTrading.explainerBefore"), {
        exact: false,
      })
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("common.liveTrading.explainerAfter"), {
        exact: false,
      })
    ).toBeInTheDocument();
  });

  it("renders NO enable/disable control (no button, switch or checkbox)", () => {
    const { container } = render(
      <LiveTradingStatus acked={false} variant="verbose" />
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(container.querySelector("button")).toBeNull();
    expect(container.querySelector("input")).toBeNull();
  });
});
