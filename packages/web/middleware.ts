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
 *       * If an `Origin` header is PRESENT (rev-web R17: present means the
 *         header exists at all — INCLUDING an explicit empty string `""`,
 *         which `req.headers.get` returns as `""` not `null`) it MUST
 *         exactly equal one of the allowed local origins:
 *         `http://localhost:<port>` or `http://127.0.0.1:<port>`, where
 *         <port> = POLYECHO_WEB_PORT (default 3000). Anything else
 *         (including `""`, `null`, a remote origin, or https) -> 403.
 *         An empty-string Origin must NOT fall through to the weaker Host
 *         path — a forged/stripped Origin is exactly the bypass we block.
 *       * ONLY when there is truly NO `Origin` header at all (`get` returns
 *         `null`) do we fall back to the `Host` header: it must be exactly
 *         `localhost:<port>` / `127.0.0.1:<port>`. A missing/foreign Host
 *         -> 403. (rev-web R17: the bare-host `localhost`/`127.0.0.1`
 *         no-port entries were removed — port 3000 is NOT an HTTP default,
 *         so a real proxy would not strip it; allowing the bare host only
 *         widened the no-Origin surface.)
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

/**
 * OPERATOR OPT-IN allow-list extension (PRD §5.9 — accepted Tailscale /
 * no-auth posture).
 *
 * The tracked default of this middleware is localhost/127.0.0.1 ONLY: that
 * secure default is UNCHANGED when this env var is unset/blank. But the
 * operator has explicitly chosen to publish the no-auth dashboard beyond
 * loopback (compose `0.0.0.0:3006`) and reach it over their own tailnet
 * (e.g. `http://mac-mini.tail3e1efb.ts.net:3006`). A browser at any origin
 * other than localhost is otherwise rejected by the strict Origin/Host
 * check, so EVERY mutating Server Action (queue backtest, save proxy, pause
 * leader, ack danger) 403s.
 *
 * `POLYECHO_WEB_ALLOWED_ORIGINS` is a comma-separated list of ABSOLUTE
 * origins (e.g. `http://mac-mini.tail3e1efb.ts.net:3006`) the operator
 * controls on their tailnet. Each is merged into BOTH the allowed-origins
 * set (matched against the `Origin` header) AND — by parsing the host:port
 * out of the origin — the allowed-hosts set (matched against `Host` on a
 * no-Origin request). This is a DELIBERATE widening of the CSRF allow-list
 * to operator-named origins; it is exactly as safe as the operator's choice
 * to expose the no-auth UI to those origins. Blank/whitespace entries and a
 * missing/empty env are ignored (behaviour == localhost-only default).
 */
function extraOrigins(): string[] {
  const raw = process.env.POLYECHO_WEB_ALLOWED_ORIGINS;
  if (!raw) return [];
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/** Host[:port] parsed out of an absolute origin, lowercased; "" if invalid. */
function hostFromOrigin(origin: string): string {
  try {
    return new URL(origin).host.toLowerCase();
  } catch {
    return "";
  }
}

/** The exact set of allowed Origin strings for this port. */
function allowedOrigins(port: string): Set<string> {
  return new Set([
    `http://localhost:${port}`,
    `http://127.0.0.1:${port}`,
    // Operator opt-in (PRD §5.9): exact origins from
    // POLYECHO_WEB_ALLOWED_ORIGINS. Empty unless the operator set it.
    ...extraOrigins(),
  ]);
}

/**
 * The exact set of allowed Host header values for this port.
 *
 * rev-web R17: the bare-host (`localhost` / `127.0.0.1` WITHOUT a port)
 * entries were removed. They were justified as "a proxy may strip the
 * default-ish port", but port 3000 is NOT an HTTP/HTTPS default (80/443) —
 * a correct reverse proxy never strips an explicit non-default port from
 * the forwarded Host. Keeping them only widened the no-Origin attack
 * surface (a local listener spoofing a bare `Host: localhost`).
 */
function allowedHosts(port: string): Set<string> {
  return new Set([
    `localhost:${port}`,
    `127.0.0.1:${port}`,
    // Operator opt-in (PRD §5.9): host:port parsed from each
    // POLYECHO_WEB_ALLOWED_ORIGINS entry. Empty unless the operator set it.
    ...extraOrigins()
      .map(hostFromOrigin)
      .filter((h) => h.length > 0),
  ]);
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
  // rev-web R17 HIGH: `Headers.get` returns `null` ONLY when the header is
  // truly absent; an explicit `Origin:` (empty value) yields `""`, NOT
  // `null`. So `origin !== null` is the correct "header is PRESENT" test.
  // A present-but-empty Origin must be treated as a non-matching Origin
  // (-> 403) and MUST NOT fall through to the weaker Host path.
  const origin = req.headers.get("origin");

  if (origin !== null) {
    // An Origin header is present (possibly `""`): it MUST exactly equal an
    // allowed local origin. `""` is not in the allow-list -> 403.
    if (!allowedOrigins(port).has(origin)) {
      return forbidden("Origin 不在允許清單");
    }
    return NextResponse.next();
  }

  // ONLY a truly ABSENT Origin reaches here: fall back to Host (must be an
  // exact local dashboard host:port — no bare-host entries any more).
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
