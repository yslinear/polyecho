import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button, buttonVariants } from "@/components/ui/button";

describe("ui/Button", () => {
  it("renders a native button with its children", () => {
    render(<Button>Click me</Button>);
    const btn = screen.getByRole("button", { name: "Click me" });
    expect(btn).toBeInTheDocument();
    expect(btn.tagName).toBe("BUTTON");
  });

  it("applies the default variant + size classes", () => {
    render(<Button>Default</Button>);
    const btn = screen.getByRole("button");
    expect(btn).toHaveClass("bg-primary", "h-10");
  });

  it("applies destructive variant and lg size classes", () => {
    render(
      <Button variant="destructive" size="lg">
        Delete
      </Button>
    );
    const btn = screen.getByRole("button");
    expect(btn).toHaveClass("bg-destructive", "h-11");
  });

  it("merges a custom className via cn()", () => {
    render(<Button className="custom-x">X</Button>);
    expect(screen.getByRole("button")).toHaveClass("custom-x");
  });

  it("fires onClick when pressed", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("does not fire onClick when disabled", async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Off
      </Button>
    );
    const btn = screen.getByRole("button");
    expect(btn).toBeDisabled();
    await userEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("renders as a child element (Slot) when asChild is set", () => {
    render(
      <Button asChild>
        <a href="/leaders">Leaders</a>
      </Button>
    );
    const link = screen.getByRole("link", { name: "Leaders" });
    expect(link).toHaveAttribute("href", "/leaders");
    // Slot forwards the button variant classes onto the anchor
    expect(link).toHaveClass("bg-primary");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("buttonVariants() produces variant-specific class strings", () => {
    expect(buttonVariants({ variant: "ghost" })).toContain("hover:bg-accent");
    expect(buttonVariants({ size: "icon" })).toContain("w-10");
  });
});
