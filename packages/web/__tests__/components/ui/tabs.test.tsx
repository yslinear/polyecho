import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
} from "@/components/ui/tabs";

function Harness() {
  return (
    <Tabs defaultValue="overview">
      <TabsList>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="stats">Stats</TabsTrigger>
        <TabsTrigger value="history">Score History</TabsTrigger>
      </TabsList>
      <TabsContent value="overview">Overview panel</TabsContent>
      <TabsContent value="stats">Stats panel</TabsContent>
      <TabsContent value="history">History panel</TabsContent>
    </Tabs>
  );
}

describe("ui/Tabs", () => {
  it("shows the default tab content and marks its trigger active", () => {
    render(<Harness />);
    expect(screen.getByText("Overview panel")).toBeVisible();
    expect(screen.queryByText("Stats panel")).not.toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Overview" })
    ).toHaveAttribute("data-state", "active");
  });

  it("switches panels when another tab is clicked", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole("tab", { name: "Stats" }));

    expect(screen.getByText("Stats panel")).toBeVisible();
    expect(screen.queryByText("Overview panel")).not.toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Stats" })
    ).toHaveAttribute("data-state", "active");
  });

  it("supports keyboard arrow navigation between tabs", async () => {
    render(<Harness />);
    const overview = screen.getByRole("tab", { name: "Overview" });
    overview.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(
      screen.getByRole("tab", { name: "Stats" })
    ).toHaveAttribute("data-state", "active");
  });
});
