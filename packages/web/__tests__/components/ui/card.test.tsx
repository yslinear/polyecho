import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "@/components/ui/card";

describe("ui/Card composition", () => {
  it("renders the full card sub-component tree", () => {
    render(
      <Card data-testid="card">
        <CardHeader>
          <CardTitle>Backtest</CardTitle>
          <CardDescription>Run summary</CardDescription>
        </CardHeader>
        <CardContent>Body</CardContent>
        <CardFooter>Footer</CardFooter>
      </Card>
    );

    expect(screen.getByTestId("card")).toHaveClass("rounded-lg", "border");
    // CardTitle renders an <h3>
    const title = screen.getByRole("heading", { name: "Backtest", level: 3 });
    expect(title).toBeInTheDocument();
    expect(screen.getByText("Run summary")).toBeInTheDocument();
    expect(screen.getByText("Body")).toBeInTheDocument();
    expect(screen.getByText("Footer")).toBeInTheDocument();
  });

  it("forwards className overrides on each part", () => {
    render(
      <Card className="c-card">
        <CardContent className="c-content">z</CardContent>
      </Card>
    );
    expect(screen.getByText("z")).toHaveClass("c-content", "p-6");
  });
});
