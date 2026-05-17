import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ScoreBadge } from "@/components/score-badge";

describe("ScoreBadge", () => {
  it("renders the label and a one-decimal value", () => {
    render(<ScoreBadge label="Total" value={72.345} />);
    expect(screen.getByText("Total")).toBeInTheDocument();
    expect(screen.getByText("72.3")).toBeInTheDocument();
  });

  it("accepts numeric strings (Drizzle numeric columns return strings)", () => {
    render(<ScoreBadge label="P" value="55.5" />);
    expect(screen.getByText("55.5")).toBeInTheDocument();
  });

  it("uses green styling for high scores (>= 70)", () => {
    const { container } = render(<ScoreBadge label="" value={85} />);
    expect(container.firstChild).toHaveClass("bg-green-100");
  });

  it("uses yellow styling for mid scores (40–69)", () => {
    const { container } = render(<ScoreBadge label="" value={50} />);
    expect(container.firstChild).toHaveClass("bg-yellow-100");
  });

  it("uses red styling for low scores (< 40)", () => {
    const { container } = render(<ScoreBadge label="" value={12} />);
    expect(container.firstChild).toHaveClass("bg-red-100");
  });

  it("boundary: exactly 70 is green and exactly 40 is yellow", () => {
    const { container: c70 } = render(<ScoreBadge label="" value={70} />);
    expect(c70.firstChild).toHaveClass("bg-green-100");
    const { container: c40 } = render(<ScoreBadge label="" value={40} />);
    expect(c40.firstChild).toHaveClass("bg-yellow-100");
  });

  it("renders a muted dash for null / undefined / NaN", () => {
    const { container: cNull } = render(
      <ScoreBadge label="" value={null} />
    );
    expect(cNull.firstChild).toHaveClass("bg-muted");
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);

    const { container: cUndef } = render(
      <ScoreBadge label="" value={undefined} />
    );
    expect(cUndef.firstChild).toHaveClass("bg-muted");

    const { container: cNaN } = render(
      <ScoreBadge label="" value="not-a-number" />
    );
    expect(cNaN.firstChild).toHaveClass("bg-muted");
  });

  it("merges a custom className", () => {
    const { container } = render(
      <ScoreBadge label="" value={1} className="extra-x" />
    );
    expect(container.firstChild).toHaveClass("extra-x");
  });
});
