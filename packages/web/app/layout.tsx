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
        <SidebarProvider>
          <AppSidebar />
          <SidebarInset>
            {/* Top header bar */}
            <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
              <SidebarTrigger className="-ml-1" />
              <div className="flex-1" />
              {/* Global Pause/Resume — Phase 4/5 placeholder */}
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5 text-xs"
                disabled
                title="Pause/Resume all copy-trading (Phase 4/5)"
              >
                <PauseCircle className="h-3.5 w-3.5" />
                Pause All
                <span className="ml-1 rounded bg-muted px-1 text-muted-foreground">
                  Phase 4/5
                </span>
              </Button>
            </header>

            <div className="flex-1 overflow-y-auto p-6">{children}</div>
          </SidebarInset>
        </SidebarProvider>
        <Toaster />
      </body>
    </html>
  );
}
