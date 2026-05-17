/**
 * Root layout — App Router.
 * No authentication: access is restricted at the network layer via Tailscale
 * (see infra/tailscale/ for ACL & Serve config). Do NOT add auth middleware.
 */

import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { NavSidebar } from "@/components/nav-sidebar";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "PolyEcho Dashboard",
  description: "Personal Polymarket copy-trading monitor",
  // PWA manifest linked for Phase 5 Web Push (not implemented yet)
  manifest: "/manifest.json",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={inter.className}>
        <div className="flex h-screen overflow-hidden">
          <NavSidebar />
          <main className="flex-1 overflow-y-auto p-8">{children}</main>
        </div>
      </body>
    </html>
  );
}
