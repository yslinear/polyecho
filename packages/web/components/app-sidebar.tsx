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
} from "@/components/ui/sidebar";
import { isNavItemActive } from "@/lib/utils";

const navItems = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/leaders", label: "Leaders", icon: Users },
  { href: "/discovery", label: "Discovery", icon: Search },
  { href: "/backtest/new", label: "Backtest", icon: FlaskConical },
  { href: "/trades", label: "Trades", icon: History },
  { href: "/positions", label: "Positions", icon: TrendingUp },
  { href: "/accounts", label: "Accounts", icon: Wallet },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function AppSidebar() {
  const pathname = usePathname();

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-1 py-1">
          <span className="text-lg font-bold tracking-tight group-data-[collapsible=icon]:hidden">
            PolyEcho
          </span>
          <span className="text-lg font-bold tracking-tight hidden group-data-[collapsible=icon]:block">
            PE
          </span>
        </div>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Navigation</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {navItems.map(({ href, label, icon: Icon }) => {
                const isActive = isNavItemActive(pathname, href);
                return (
                  <SidebarMenuItem key={href}>
                    <SidebarMenuButton
                      asChild
                      isActive={isActive}
                      tooltip={label}
                    >
                      <Link href={href}>
                        <Icon />
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
          v0.3 · localhost only
        </div>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}
