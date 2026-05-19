import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ScoreBadge } from "@/components/score-badge";
import { t } from "@/lib/i18n";

// Tier labels are derived from the i18n dictionary, not hardcoded English:
// the test asserts the tier↔token↔colour mapping (the load-bearing
// behaviour) and stays correct if a translation string changes.
const HIGH = t("common.scoreBadge.high");
const MID = t("common.scoreBadge.mid");
const LOW = t("common.scoreBadge.low");
const NOT_SET = t("common.scoreBadge.notSet");

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

  it("uses the gain token + a High text tier for high scores (>= 70)", () => {
    const { container } = render(<ScoreBadge label="" value={85} />);
    expect(container.firstChild).toHaveClass("text-gain");
    expect(screen.getByText(HIGH)).toBeInTheDocument();
  });

  it("uses the muted token + a Mid text tier for mid scores (40–69)", () => {
    const { container } = render(<ScoreBadge label="" value={50} />);
    expect(container.firstChild).toHaveClass("text-muted-foreground");
    expect(screen.getByText(MID)).toBeInTheDocument();
  });

  it("uses the loss token + a Low text tier for low scores (< 40)", () => {
    const { container } = render(<ScoreBadge label="" value={12} />);
    expect(container.firstChild).toHaveClass("text-loss");
    expect(screen.getByText(LOW)).toBeInTheDocument();
  });

  it("boundary: exactly 70 is High (gain) and exactly 40 is Mid (muted)", () => {
    const { container: c70 } = render(<ScoreBadge label="" value={70} />);
    expect(c70.firstChild).toHaveClass("text-gain");
    expect(screen.getByText(HIGH)).toBeInTheDocument();
    const { container: c40 } = render(<ScoreBadge label="" value={40} />);
    expect(c40.firstChild).toHaveClass("text-muted-foreground");
    expect(screen.getByText(MID)).toBeInTheDocument();
  });

  it("renders a muted textual placeholder for null / undefined / NaN", () => {
    const { container: cNull } = render(
      <ScoreBadge label="" value={null} />
    );
    expect(cNull.firstChild).toHaveClass("bg-muted");
    expect(screen.getAllByText(NOT_SET).length).toBeGreaterThan(0);

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
