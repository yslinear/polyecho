import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, badgeVariants } from "@/components/ui/badge";

describe("ui/Badge", () => {
  it("renders its content", () => {
    render(<Badge>NEW</Badge>);
    expect(screen.getByText("NEW")).toBeInTheDocument();
  });

  it("uses the default variant by default", () => {
    render(<Badge>x</Badge>);
    expect(screen.getByText("x")).toHaveClass("bg-primary");
  });

  it.each([
    ["secondary", "bg-secondary"],
    ["destructive", "bg-destructive"],
    ["outline", "text-foreground"],
  ] as const)("variant=%s applies %s", (variant, cls) => {
    render(<Badge variant={variant}>v</Badge>);
    expect(screen.getByText("v")).toHaveClass(cls);
  });

  it("merges custom className", () => {
    render(<Badge className="px-9">y</Badge>);
    expect(screen.getByText("y")).toHaveClass("px-9");
  });

  it("badgeVariants() exposes variant classes", () => {
    expect(badgeVariants({ variant: "destructive" })).toContain(
      "bg-destructive"
    );
  });
});
