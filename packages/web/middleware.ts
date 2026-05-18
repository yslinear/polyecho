/**
 * P0-2 — Origin allow-list (CSRF / cross-origin POST defence).
 *
 * Threat model: the PolyEcho dashboard is a localhost-only, no-auth
 * single-operator UI. That model is ONLY safe if no untrusted local code is
 * running on the operator's machine — a malicious page open in the same
 * browser could otherwise drive Server Actions (which are POSTs to the app's
 * own routes) cross-origin. This middleware rejects any MUTATING request
 * whose `Origin` is not the configured local dashboard origin.
 *
 * Rule (minimal, must not break normal dashboard use):
 *   - Non-mutating methods (GET/HEAD/OPTIONS) -> always allowed. Top-level
 *     navigations send no usable `Origin` for GET, and Next RSC/prefetch
 *     GETs are same-origin reads; gating them would break the dashboard.
 *   - Mutating methods (POST/PUT/PATCH/DELETE — Server Action invocations
 *     are POSTs):
 *       * If an `Origin` header is present it MUST exactly equal one of the
 *         allowed local origins: `http://localhost:<port>` or
 *         `http://127.0.0.1:<port>`, where <port> = POLYECHO_WEB_PORT
 *         (default 3000). Anything else (including `null`, a remote origin,
 *         or https) -> 403.
 *       * If there is NO `Origin` header, fall back to the `Host` header: it
 *         must be `localhost:<port>` / `127.0.0.1:<port>` (or the bare host
 *         when the port is the default). A missing/foreign Host -> 403.
 *
 * Same-origin browser fetches always send `Origin: <self>` for POST, so the
 * legitimate dashboard keeps working; a cross-site POST carries the
 * attacker's Origin (or a forbidden `null`) and is blocked.
 *
 * Residual risk (documented): this is an Origin/Host allow-list, not auth.
 * The localhost-no-auth model still REQUIRES that no untrusted local code
 * runs on the host (it could forge Host or run a local listener). Header
 * spoofing is not possible from browser JS for `Origin`, but a native local
 * process is outside this control.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Configured dashboard port (POLYECHO_WEB_PORT, default 3000). */
function webPort(): string {
  const raw = process.env.POLYECHO_WEB_PORT;
  const n = raw ? Number.parseInt(raw, 10) : NaN;
  if (!Number.isFinite(n) || n < 1 || n > 65535) return "3000";
  return String(n);
}

/** The exact set of allowed Origin strings for this port. */
function allowedOrigins(port: string): Set<string> {
  return new Set([
    `http://localhost:${port}`,
    `http://127.0.0.1:${port}`,
  ]);
}

/** The exact set of allowed Host header values for this port. */
function allowedHosts(port: string): Set<string> {
  const hosts = new Set([
    `localhost:${port}`,
    `127.0.0.1:${port}`,
  ]);
  if (port === "3000") {
    // A proxy may strip the default-ish port; accept the bare host too.
    hosts.add("localhost");
    hosts.add("127.0.0.1");
  }
  return hosts;
}

function forbidden(reason: string): NextResponse {
  return new NextResponse(
    `跨來源的變更請求已被拒絕（${reason}）。本儀表板僅接受本機同源操作。`,
    { status: 403, headers: { "content-type": "text/plain; charset=utf-8" } }
  );
}

export function middleware(req: NextRequest): NextResponse {
  const method = req.method.toUpperCase();

  // Non-mutating reads/navigations are always allowed.
  if (!MUTATING.has(method)) return NextResponse.next();

  const port = webPort();
  const origin = req.headers.get("origin");

  if (origin !== null && origin !== "") {
    // An Origin header is present: it MUST be an exact allowed local origin.
    if (!allowedOrigins(port).has(origin)) {
      return forbidden("Origin 不在允許清單");
    }
    return NextResponse.next();
  }

  // No Origin header: fall back to Host (must be a local dashboard host).
  const host = req.headers.get("host");
  if (!host || !allowedHosts(port).has(host.toLowerCase())) {
    return forbidden("缺少 Origin 且 Host 不是本機儀表板");
  }
  return NextResponse.next();
}

/**
 * Run on every route EXCEPT Next internals & static assets — those are
 * GET-only anyway, excluding them keeps the matcher cheap. Server Actions
 * and API/page POSTs all fall under this matcher.
 */
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
