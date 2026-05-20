"use client";

/**
 * AppSidebar — shadcn/ui collapsible sidebar navigation.
 * Server/Client island: uses useSidebar for collapse state.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Users,
  Search,
  FlaskConical,
  History,
  TrendingUp,
  Wallet,
  Bell,
  Activity,
  Settings,
} from "lucide-react";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  useSidebar,
} from "@/components/ui/sidebar";
import { t } from "@/lib/i18n";
import { isNavItemActive } from "@/lib/utils";

const navItems = [
  { href: "/", labelKey: "sidebar.items.dashboard", icon: LayoutDashboard },
  { href: "/leaders", labelKey: "sidebar.items.leaders", icon: Users },
  { href: "/discovery", labelKey: "sidebar.items.discovery", icon: Search },
  {
    // 2026-05-21: now points at the LIST page; the list has a prominent
    // 「新增回測」 button leading into `/backtest/new`. The sidebar active-
    // state contract still relies on `navSection("/backtest")` matching
    // both `/backtest`, `/backtest/new`, and `/backtest/<id>`.
    href: "/backtest",
    labelKey: "sidebar.items.backtest",
    icon: FlaskConical,
  },
  { href: "/trades", labelKey: "sidebar.items.trades", icon: History },
  { href: "/positions", labelKey: "sidebar.items.positions", icon: TrendingUp },
  { href: "/accounts", labelKey: "sidebar.items.accounts", icon: Wallet },
  { href: "/alerts", labelKey: "sidebar.items.alerts", icon: Bell },
  { href: "/status", labelKey: "sidebar.items.status", icon: Activity },
  { href: "/settings", labelKey: "sidebar.items.settings", icon: Settings },
] as const;

export function AppSidebar() {
  const pathname = usePathname();
  // Close the mobile off-canvas drawer on navigation — without this the
  // full-screen Sheet + scrim stay open over the new page on every tap
  // (the dominant cause of the poor phone UX).  No-op on desktop.
  const { setOpenMobile } = useSidebar();

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex flex-col gap-0.5 px-1 py-1">
          <span className="text-lg font-bold tracking-tight group-data-[collapsible=icon]:hidden">
            {/* Product brand name — proper noun per common.ts
                PROPER-NOUN POLICY, never translated. */}
            {"PolyEcho"}
          </span>
          <span className="hidden text-lg font-bold tracking-tight group-data-[collapsible=icon]:block">
            PE
          </span>
        </div>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>{t("sidebar.nav")}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu aria-label={t("sidebar.navAria")}>
              {navItems.map(({ href, labelKey, icon: Icon }) => {
                const isActive = isNavItemActive(pathname, href);
                const label = t(labelKey);
                return (
                  <SidebarMenuItem key={href}>
                    <SidebarMenuButton
                      asChild
                      isActive={isActive}
                      tooltip={label}
                    >
                      <Link
                        href={href}
                        aria-current={isActive ? "page" : undefined}
                        onClick={() => setOpenMobile(false)}
                      >
                        <Icon aria-hidden="true" />
                        <span>{label}</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <div className="px-2 py-1 text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">
          {t("sidebar.footer")}
        </div>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}
