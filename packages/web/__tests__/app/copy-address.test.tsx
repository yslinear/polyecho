import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CopyAddress } from "@/app/accounts/copy-address";

describe("CopyAddress (client island)", () => {
  beforeEach(() => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  it("renders the shortened address with full address as title", () => {
    const addr = "0xabcdef0123456789abcdef0123456789abcdef01";
    render(<CopyAddress address={addr} />);
    const short = screen.getByText("0xabcd…ef01");
    expect(short).toBeInTheDocument();
    expect(short).toHaveAttribute("title", addr);
  });

  it("copies the FULL address to the clipboard on click", async () => {
    const addr = "0xabcdef0123456789abcdef0123456789abcdef01";
    render(<CopyAddress address={addr} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Copy address" })
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(addr);
    // Button label flips to "Copied" after a successful copy.
    expect(
      await screen.findByRole("button", { name: "Copied" })
    ).toBeInTheDocument();
  });

  it("does not throw when the clipboard API is unavailable", async () => {
    Object.assign(navigator, { clipboard: undefined });
    render(
      <CopyAddress address="0x1111111111111111111111111111111111111111" />
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Copy address" })
    );
    // Still rendered, no crash.
    expect(
      screen.getByRole("button", { name: "Copy address" })
    ).toBeInTheDocument();
  });
});
