"use client";

/**
 * AppSidebar — shadcn/ui collapsible sidebar navigation.
 *
 * The 10 destinations are clustered into labelled groups (探索與評估 /
 * 交易與帳戶 / 監控) so the nav reads as functional sections instead of one
 * flat run; the lead Dashboard sits in an unlabelled group above them.
 * Settings is pinned to the footer (the standard "config at the bottom"
 * affordance).
 *
 * Collapse trigger: on desktop it lives in the SidebarHeader, co-located
 * with the panel it controls. The mobile off-canvas trigger stays in the
 * page header (layout.tsx) — the Sheet is invisible when closed, so its
 * trigger cannot live inside it.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Users,
  Search,
  FlaskConical,
  Microscope,
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
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { t } from "@/lib/i18n";
import { isNavItemActive } from "@/lib/utils";

// Nav clusters. The first group is intentionally label-less (Dashboard is
// THE home, it needs no section heading). `as const` keeps every labelKey a
// literal so `t()` accepts it without an `I18nKey` cast.
const NAV_GROUPS = [
  {
    items: [
      { href: "/", labelKey: "sidebar.items.dashboard", icon: LayoutDashboard },
    ],
  },
  {
    labelKey: "sidebar.groups.discovery",
    items: [
      { href: "/leaders", labelKey: "sidebar.items.leaders", icon: Users },
      { href: "/discovery", labelKey: "sidebar.items.discovery", icon: Search },
      // Section "/backtest" matches /backtest, /backtest/new and
      // /backtest/<id> — see isNavItemActive.
      { href: "/backtest", labelKey: "sidebar.items.backtest", icon: FlaskConical },
      // C1-P5: the forward paper-trading ladder (lifecycle_state + paper
      // stats + operator-gated promote-to-live). Belongs in this group —
      // it is the evaluation stage between discovery and a real follow.
      { href: "/research", labelKey: "sidebar.items.research", icon: Microscope },
    ],
  },
  {
    labelKey: "sidebar.groups.trading",
    items: [
      { href: "/trades", labelKey: "sidebar.items.trades", icon: History },
      { href: "/positions", labelKey: "sidebar.items.positions", icon: TrendingUp },
      { href: "/accounts", labelKey: "sidebar.items.accounts", icon: Wallet },
    ],
  },
  {
    labelKey: "sidebar.groups.monitoring",
    items: [
      { href: "/alerts", labelKey: "sidebar.items.alerts", icon: Bell },
      { href: "/status", labelKey: "sidebar.items.status", icon: Activity },
    ],
  },
] as const;

export function AppSidebar() {
  const pathname = usePathname();
  // Close the mobile off-canvas drawer on navigation — without this the
  // full-screen Sheet + scrim stay open over the new page on every tap.
  // No-op on desktop.
  const { setOpenMobile } = useSidebar();

  const renderItem = (href: string, label: string, Icon: typeof Settings) => {
    const isActive = isNavItemActive(pathname, href);
    return (
      <SidebarMenuItem key={href}>
        <SidebarMenuButton asChild isActive={isActive} tooltip={label}>
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
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        {/* Brand + desktop collapse trigger on one row. When collapsed to
            the icon rail the brand hides and the trigger centres. */}
        <div className="flex items-center justify-between gap-2 px-1 py-1 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0">
          <span className="text-lg font-bold tracking-tight group-data-[collapsible=icon]:hidden">
            {/* Product brand name — proper noun per common.ts
                PROPER-NOUN POLICY, never translated. */}
            {"PolyEcho"}
          </span>
          <SidebarTrigger
            className="hidden size-7 shrink-0 focus-visible:ring-2 focus-visible:ring-ring md:flex"
            aria-label={t("common.a11y.toggleSidebar")}
          />
        </div>
      </SidebarHeader>

      <SidebarContent>
        {NAV_GROUPS.map((group, gi) => {
          const groupLabel =
            "labelKey" in group ? t(group.labelKey) : t("sidebar.navAria");
          return (
            <SidebarGroup key={gi}>
              {"labelKey" in group && (
                <SidebarGroupLabel>{groupLabel}</SidebarGroupLabel>
              )}
              <SidebarGroupContent>
                <SidebarMenu aria-label={groupLabel}>
                  {group.items.map(({ href, labelKey, icon }) =>
                    renderItem(href, t(labelKey), icon),
                  )}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          );
        })}
      </SidebarContent>

      <SidebarFooter>
        {/* Settings is pinned here — the conventional "config lives at the
            bottom" affordance, kept out of the functional nav clusters. */}
        <SidebarMenu aria-label={t("sidebar.items.settings")}>
          {renderItem("/settings", t("sidebar.items.settings"), Settings)}
        </SidebarMenu>
        <div className="px-2 py-1 text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">
          {t("sidebar.footer")}
        </div>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}
