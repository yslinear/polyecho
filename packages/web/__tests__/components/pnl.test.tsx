import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Pnl, formatPnl, pnlDirection } from "@/components/pnl";

describe("formatPnl", () => {
  it("adds explicit + only for positive, never for zero/null", () => {
    expect(formatPnl("12")).toBe("+$12.00");
    expect(formatPnl("-12")).toBe("-$12.00");
    expect(formatPnl(0)).toBe("$0.00");
    expect(formatPnl(null)).toBe("$0.00");
    expect(formatPnl(undefined)).toBe("$0.00");
  });
  it("accepts Drizzle numeric strings", () => {
    expect(formatPnl("1234.5")).toBe("+$1,234.50");
  });
});

describe("pnlDirection", () => {
  it("classifies up / down / flat", () => {
    expect(pnlDirection("5")).toBe("up");
    expect(pnlDirection(-5)).toBe("down");
    expect(pnlDirection(0)).toBe("flat");
    expect(pnlDirection(null)).toBe("flat");
    expect(pnlDirection("not-a-number")).toBe("flat");
  });
});

describe("<Pnl />", () => {
  it("renders gain tone + up arrow + signed figure for positive", () => {
    render(<Pnl value="42.5" />);
    const el = screen.getByText(/\+\$42\.50/);
    expect(el).toHaveClass("text-gain");
    expect(el).toHaveClass("tabular-nums");
    expect(el).toHaveAttribute("data-pnl", "up");
  });

  it("renders loss tone for negative", () => {
    render(<Pnl value={-9} />);
    const el = screen.getByText(/-\$9\.00/);
    expect(el).toHaveClass("text-loss");
    expect(el).toHaveAttribute("data-pnl", "down");
  });

  it("is neutral (muted, no judgement) for zero and null", () => {
    const { rerender } = render(<Pnl value={0} />);
    let el = screen.getByText(/\$0\.00/);
    expect(el).toHaveClass("text-muted-foreground");
    expect(el).toHaveAttribute("data-pnl", "flat");

    rerender(<Pnl value={null} />);
    el = screen.getByText(/\$0\.00/);
    expect(el).toHaveClass("text-muted-foreground");
  });

  it("the arrow is aria-hidden so the signed figure is the sole a11y signal", () => {
    const { container } = render(<Pnl value="3" />);
    const arrow = container.querySelector('[aria-hidden="true"]');
    expect(arrow).not.toBeNull();
  });

  it("hideArrow drops the glyph but keeps the signed figure", () => {
    const { container } = render(<Pnl value="3" hideArrow />);
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull();
    expect(screen.getByText(/\+\$3\.00/)).toBeInTheDocument();
  });

  it("metric emphasis bumps weight without changing semantics", () => {
    render(<Pnl value="1" emphasis="metric" />);
    const el = screen.getByText(/\+\$1\.00/);
    expect(el).toHaveClass("font-semibold");
  });
});
