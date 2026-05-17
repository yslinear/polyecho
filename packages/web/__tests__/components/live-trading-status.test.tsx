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

describe("LiveTradingStatus", () => {
  it("shows LOCKED when there is no live_trading_ack", () => {
    render(<LiveTradingStatus acked={false} />);
    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      "Live trading LOCKED"
    );
    expect(screen.getByText(/Live LOCKED/)).toBeInTheDocument();
  });

  it("shows UNLOCKED when live_trading_ack exists", () => {
    render(<LiveTradingStatus acked={true} />);
    expect(screen.getByRole("status")).toHaveAttribute(
      "aria-label",
      "Live trading UNLOCKED"
    );
    expect(screen.getByText(/Live UNLOCKED/)).toBeInTheDocument();
  });

  it("verbose variant names POLYECHO_LIVE_TRADING as the CLI-only switch", () => {
    render(<LiveTradingStatus acked={false} variant="verbose" />);
    expect(screen.getByText("POLYECHO_LIVE_TRADING")).toBeInTheDocument();
    expect(
      screen.getByText(/read-only indicator/i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/changed on the CLI only/i)
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
