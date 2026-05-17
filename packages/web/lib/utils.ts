import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Normalize a path: strip query/hash, collapse a trailing slash (except root).
 *   "/backtest/" -> "/backtest", "/" -> "/", "/x?q=1#h" -> "/x"
 */
function normalizePath(path: string): string {
  const noQuery = path.split(/[?#]/, 1)[0] || "/";
  if (noQuery.length > 1 && noQuery.endsWith("/")) {
    return noQuery.replace(/\/+$/, "") || "/";
  }
  return noQuery;
}

/**
 * Derive the top-level "section" of a nav href so detail routes highlight the
 * right sidebar item. A nav href may point at a sub-route (e.g. "/backtest/new")
 * while the section it represents is the first segment ("/backtest"). The root
 * href "/" is its own section.
 *
 *   "/"             -> "/"
 *   "/discovery"    -> "/discovery"
 *   "/backtest/new" -> "/backtest"
 */
export function navSection(href: string): string {
  const normalized = normalizePath(href);
  if (normalized === "/") return "/";
  const firstSegment = normalized.split("/").filter(Boolean)[0];
  return firstSegment ? `/${firstSegment}` : "/";
}

/**
 * Section-match for sidebar active state.
 *
 * - Root ("/") matches ONLY the exact root path (no false "matches everything").
 * - Every other item matches its section and any nested route, but only on a
 *   path-segment boundary so "/trades" does NOT match "/trades-extra" and
 *   "/discovery" still owns "/discovery/0xabc".
 *
 * The href is reduced to its top-level section first, so a nav item whose href
 * is a sub-route ("/backtest/new") is highlighted for the whole section
 * ("/backtest", "/backtest/new", "/backtest/<id>").
 */
export function isNavItemActive(pathname: string, href: string): boolean {
  const path = normalizePath(pathname);
  const section = navSection(href);

  if (section === "/") return path === "/";

  return path === section || path.startsWith(`${section}/`);
}
