/**
 * NavSidebar — shared navigation for all dashboard pages.
 * Server Component; no auth required (Tailscale network isolation handles access control).
 */

import Link from "next/link";
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
import { cn } from "@/lib/utils";

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

interface NavSidebarProps {
  className?: string;
}

export function NavSidebar({ className }: NavSidebarProps) {
  return (
    <aside
      className={cn(
        "flex flex-col w-56 shrink-0 border-r bg-card px-3 py-6 gap-1",
        className
      )}
    >
      <div className="mb-6 px-3">
        <span className="text-xl font-bold tracking-tight">PolyEcho</span>
      </div>
      <nav className="flex flex-col gap-1">
        {navItems.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            <Icon className="h-4 w-4" />
            {label}
          </Link>
        ))}
      </nav>
    </aside>
  );
}
