/**
 * Root layout — App Router.
 * No authentication by design: the dashboard binds to 127.0.0.1 only and is
 * never exposed to the network. Private/remote access is the operator's
 * responsibility, out of project scope. Do NOT add auth middleware.
 */

import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { eq } from "drizzle-orm";

import { AppSidebar } from "@/components/app-sidebar";
import { SidebarProvider, SidebarTrigger, SidebarInset } from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/sonner";
import { GlobalPauseToggle } from "@/components/global-pause-toggle";
import { db } from "@/lib/db";
import { settings } from "@/lib/db/schema";
import { GLOBAL_PAUSED_KEY, coerceBoolSetting } from "@/lib/settings-keys";
import { t } from "@/lib/i18n";

const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

export const metadata: Metadata = {
  title: "PolyEcho Dashboard",
  description: "Personal Polymarket copy-trading monitor",
  // PWA manifest is served by app/manifest.ts; Next injects the
  // <link rel="manifest"> automatically — do NOT hard-code it here.
};

/**
 * The manifest declares `display: standalone`, so the operator runs this
 * as a home-screen PWA on their phone. `viewportFit: "cover"` lets the
 * shell use `env(safe-area-inset-*)` so the header/trigger clear the
 * notch/Dynamic Island and the mobile drawer clears the home indicator.
 */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

/**
 * The shell reads the global-pause settings row, so the layout must be
 * dynamic (never statically prerendered). This matches every data page's
 * `export const dynamic = "force-dynamic"` and stops the build from trying
 * to hit Postgres while prerendering static routes like /_not-found.
 */
export const dynamic = "force-dynamic";

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Resilient read: a transient DB outage must never blank the whole app
  // shell — fall back to "not paused" and let the page-level reads surface
  // the real error.
  let globalPaused = false;
  try {
    const [pauseRow] = await db
      .select({ value: settings.value })
      .from(settings)
      .where(eq(settings.key, GLOBAL_PAUSED_KEY))
      .limit(1);
    globalPaused = coerceBoolSetting(pauseRow?.value);
  } catch {
    globalPaused = false;
  }

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
          {t("common.a11y.skipToContent")}
        </a>
        <SidebarProvider>
          <AppSidebar />
          <SidebarInset>
            {/* Top header bar — calm, instrument chrome. */}
            <header className="flex min-h-12 shrink-0 items-center gap-3 border-b bg-background/95 px-4 pt-[env(safe-area-inset-top)] supports-[backdrop-filter]:bg-background/80">
              <SidebarTrigger
                className="-ml-1 size-9 md:size-7 focus-visible:ring-2 focus-visible:ring-ring"
                aria-label="Toggle navigation sidebar"
              />
              <div className="flex-1" />
              {/* Global 全域暫停/恢復 — PRD §5.8 one-click safe write. */}
              <GlobalPauseToggle paused={globalPaused} />
            </header>

            <main
              id="main"
              className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto p-6"
            >
              {children}
            </main>
          </SidebarInset>
        </SidebarProvider>
        <Toaster />
      </body>
    </html>
  );
}
