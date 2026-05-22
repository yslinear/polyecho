"use client";

/**
 * CopyAddress — client island: shows a shortened 0x address and a
 * copy-to-clipboard button. Read-only; never touches private keys.
 */

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { shortAddress } from "@/lib/format";
import { t } from "@/lib/i18n";

interface CopyAddressProps {
  address: string;
}

const COPY_LABELS = {
  copied: t("tables.accounts.copied"),
  copy: t("tables.accounts.copyAddress"),
} as const;

export function CopyAddress({ address }: CopyAddressProps) {
  const [copied, setCopied] = React.useState(false);

  async function handleCopy() {
    try {
      if (!navigator.clipboard?.writeText) return;
      await navigator.clipboard.writeText(address);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard may be unavailable (e.g. non-secure context); ignore.
    }
  }

  return (
    <span className="inline-flex items-center gap-0.5">
      <span className="font-mono text-xs" title={address}>
        {shortAddress(address)}
      </span>
      {/* The icon stays visually small (h-3 w-3) but the button is a full
          44x44px touch target (DESIGN.md / a11y tap-target minimum). The
          negative margin keeps the visual row tight so the larger hit
          area does not bloat the table cell. */}
      <Button
        variant="ghost"
        size="icon"
        className="-my-2 h-11 w-11"
        onClick={handleCopy}
        aria-label={copied ? COPY_LABELS.copied : COPY_LABELS.copy}
      >
        {copied ? (
          <Check className="h-3 w-3 text-gain" />
        ) : (
          <Copy className="h-3 w-3" />
        )}
      </Button>
    </span>
  );
}
