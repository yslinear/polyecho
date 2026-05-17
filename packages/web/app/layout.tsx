/**
 * Root layout — App Router.
 * No authentication by design: the dashboard binds to 127.0.0.1 only and is
 * never exposed to the network. Private/remote access is the operator's
 * responsibility, out of project scope. Do NOT add auth middleware.
 */

import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { AppSidebar } from "@/components/app-sidebar";
import { SidebarProvider, SidebarTrigger, SidebarInset } from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { PauseCircle } from "lucide-react";

const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

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
      <body
        className={`${inter.variable} ${inter.className} antialiased`}
        suppressHydrationWarning
      >
        {/* Keyboard users skip the persistent nav straight to content. */}
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-3 focus:z-50 focus:rounded-md focus:bg-card focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:shadow-sm focus:outline-none focus:ring-2 focus:ring-ring"
        >
          Skip to content
        </a>
        <SidebarProvider>
          <AppSidebar />
          <SidebarInset>
            {/* Top header bar — calm, instrument chrome. */}
            <header className="flex h-12 shrink-0 items-center gap-3 border-b bg-background/95 px-4 supports-[backdrop-filter]:bg-background/80">
              <SidebarTrigger
                className="-ml-1 focus-visible:ring-2 focus-visible:ring-ring"
                aria-label="Toggle navigation sidebar"
              />
              <div className="flex-1" />
              {/* Global Pause/Resume — Phase 4/5 placeholder (disabled). */}
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5 text-xs transition-colors duration-180 ease-calm"
                disabled
                aria-label="Pause or resume all copy-trading (available in Phase 4/5)"
                title="Pause/Resume all copy-trading (Phase 4/5)"
              >
                <PauseCircle className="h-3.5 w-3.5" aria-hidden="true" />
                Pause All
                <span className="ml-1 rounded bg-muted px-1 font-medium text-muted-foreground">
                  Phase 4/5
                </span>
              </Button>
            </header>

            <main id="main" className="flex-1 overflow-y-auto p-6">
              {children}
            </main>
          </SidebarInset>
        </SidebarProvider>
        <Toaster />
      </body>
    </html>
  );
}
