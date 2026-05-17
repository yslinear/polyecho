import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from "@/components/ui/dialog";

function Harness() {
  return (
    <Dialog>
      <DialogTrigger>Open confirm</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Confirm dangerous action</DialogTitle>
          <DialogDescription>This cannot be undone.</DialogDescription>
        </DialogHeader>
        <DialogClose>Cancel</DialogClose>
      </DialogContent>
    </Dialog>
  );
}

describe("ui/Dialog", () => {
  it("is closed initially", () => {
    render(<Harness />);
    expect(
      screen.queryByText("Confirm dangerous action")
    ).not.toBeInTheDocument();
  });

  it("opens on trigger click and exposes a dialog role", async () => {
    render(<Harness />);
    await userEvent.click(
      screen.getByRole("button", { name: "Open confirm" })
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(
      screen.getByText("Confirm dangerous action")
    ).toBeInTheDocument();
    expect(screen.getByText("This cannot be undone.")).toBeInTheDocument();
  });

  it("closes when the close control is clicked", async () => {
    render(<Harness />);
    await userEvent.click(
      screen.getByRole("button", { name: "Open confirm" })
    );
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    render(<Harness />);
    await userEvent.click(
      screen.getByRole("button", { name: "Open confirm" })
    );
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
