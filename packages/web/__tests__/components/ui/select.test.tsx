import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

function Harness({ onValueChange }: { onValueChange?: (v: string) => void }) {
  return (
    <Select onValueChange={onValueChange}>
      <SelectTrigger aria-label="Scenario">
        <SelectValue placeholder="Select scenario" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="triple">Triple</SelectItem>
        <SelectItem value="optimistic">Optimistic</SelectItem>
        <SelectItem value="pessimistic">Pessimistic</SelectItem>
      </SelectContent>
    </Select>
  );
}

describe("ui/Select", () => {
  it("shows the placeholder before any selection", () => {
    render(<Harness />);
    expect(screen.getByText("Select scenario")).toBeInTheDocument();
  });

  it("opens the listbox and selects an option, firing onValueChange", async () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);

    await userEvent.click(screen.getByRole("combobox", { name: "Scenario" }));
    const option = await screen.findByRole("option", { name: "Optimistic" });
    await userEvent.click(option);

    expect(onValueChange).toHaveBeenCalledWith("optimistic");
  });
});
