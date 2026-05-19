import { describe, it, expect, beforeEach } from "vitest";
import { NextRequest } from "next/server";

/**
 * P0-2 Origin allow-list middleware.
 *
 * Contract:
 *  - GET / HEAD / OPTIONS are always allowed (status passthrough).
 *  - A mutating POST with a cross-origin `Origin` -> 403.
 *  - A mutating POST with the configured local origin -> allowed.
 *  - A mutating POST with NO Origin but a local Host -> allowed; foreign
 *    Host -> 403.
 *  - The allowed port follows POLYECHO_WEB_PORT (default 3000).
 */

import { middleware } from "@/middleware";

function req(
  method: string,
  headers: Record<string, string>
): NextRequest {
  return new NextRequest("http://localhost:3000/alerts", {
    method,
    headers,
  });
}

beforeEach(() => {
  delete process.env.POLYECHO_WEB_PORT;
  delete process.env.POLYECHO_WEB_ALLOWED_ORIGINS;
});

describe("middleware — Origin allow-list", () => {
  it("allows GET navigations regardless of Origin", () => {
    const res = middleware(req("GET", { origin: "https://evil.example" }));
    // NextResponse.next() -> a passthrough response, NOT a 403.
    expect(res.status).not.toBe(403);
  });

  it("allows HEAD and OPTIONS", () => {
    expect(middleware(req("HEAD", {})).status).not.toBe(403);
    expect(middleware(req("OPTIONS", {})).status).not.toBe(403);
  });

  it("BLOCKS a cross-origin POST (Server Action invocation)", () => {
    const res = middleware(
      req("POST", { origin: "https://evil.example", host: "localhost:3000" })
    );
    expect(res.status).toBe(403);
  });

  it("blocks a POST whose Origin is https on the same host", () => {
    const res = middleware(
      req("POST", { origin: "https://localhost:3000", host: "localhost:3000" })
    );
    expect(res.status).toBe(403);
  });

  it("ALLOWS a same-origin POST (localhost:3000)", () => {
    const res = middleware(
      req("POST", { origin: "http://localhost:3000", host: "localhost:3000" })
    );
    expect(res.status).not.toBe(403);
  });

  it("ALLOWS a same-origin POST (127.0.0.1:3000)", () => {
    const res = middleware(
      req("POST", { origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000" })
    );
    expect(res.status).not.toBe(403);
  });

  it("allows a POST with NO Origin but a local Host", () => {
    const res = middleware(req("POST", { host: "127.0.0.1:3000" }));
    expect(res.status).not.toBe(403);
  });

  it("blocks a POST with NO Origin and a foreign Host", () => {
    const res = middleware(req("POST", { host: "evil.example" }));
    expect(res.status).toBe(403);
  });

  it("blocks a POST with neither Origin nor Host", () => {
    const res = middleware(req("POST", {}));
    expect(res.status).toBe(403);
  });

  // ---- rev-web R17 HIGH regressions -------------------------------------

  it("BLOCKS a POST whose Origin header is PRESENT but empty (no Host fallthrough)", () => {
    // An explicit empty `Origin:` must NOT bypass the strict Origin check by
    // falling through to the weaker Host path — even with a perfectly valid
    // local Host. Header present (even "") => must exactly match an origin.
    const res = middleware(
      req("POST", { origin: "", host: "localhost:3000" })
    );
    expect(res.status).toBe(403);
  });

  it("BLOCKS a POST with NO Origin and a bare Host (no port) on default 3000", () => {
    // Port 3000 is not an HTTP default; a real proxy would not strip it, so
    // the bare-host allow entries were removed. Bare `localhost` -> 403.
    expect(middleware(req("POST", { host: "localhost" })).status).toBe(403);
    expect(middleware(req("POST", { host: "127.0.0.1" })).status).toBe(403);
  });

  it("honours POLYECHO_WEB_PORT for the allowed origin", () => {
    process.env.POLYECHO_WEB_PORT = "4321";
    const ok = middleware(
      req("POST", { origin: "http://localhost:4321", host: "localhost:4321" })
    );
    expect(ok.status).not.toBe(403);
    const bad = middleware(
      req("POST", { origin: "http://localhost:3000", host: "localhost:3000" })
    );
    expect(bad.status).toBe(403);
  });

  // ---- operator-opt-in CSRF allow-list extension (PRD §5.9) --------------

  it("default (no POLYECHO_WEB_ALLOWED_ORIGINS) still 403s a tailnet Origin", () => {
    // The secure tracked default is UNCHANGED: a non-localhost Origin is
    // rejected when the operator has NOT opted in.
    const res = middleware(
      req("POST", {
        origin: "http://mac-mini.tail3e1efb.ts.net:3006",
        host: "mac-mini.tail3e1efb.ts.net:3006",
      })
    );
    expect(res.status).toBe(403);
  });

  it("ALLOWS a mutating POST from an origin listed in POLYECHO_WEB_ALLOWED_ORIGINS", () => {
    process.env.POLYECHO_WEB_ALLOWED_ORIGINS =
      "http://mac-mini.tail3e1efb.ts.net:3006";
    const res = middleware(
      req("POST", {
        origin: "http://mac-mini.tail3e1efb.ts.net:3006",
        host: "mac-mini.tail3e1efb.ts.net:3006",
      })
    );
    expect(res.status).not.toBe(403);
  });

  it("ALLOWS a no-Origin POST whose Host matches a POLYECHO_WEB_ALLOWED_ORIGINS entry", () => {
    process.env.POLYECHO_WEB_ALLOWED_ORIGINS =
      "http://mac-mini.tail3e1efb.ts.net:3006";
    const res = middleware(
      req("POST", { host: "mac-mini.tail3e1efb.ts.net:3006" })
    );
    expect(res.status).not.toBe(403);
  });

  it("parses multiple comma-separated origins and trims blanks", () => {
    process.env.POLYECHO_WEB_ALLOWED_ORIGINS =
      " http://a.ts.net:3006 , , http://b.ts.net:3006 ";
    expect(
      middleware(
        req("POST", { origin: "http://a.ts.net:3006", host: "a.ts.net:3006" })
      ).status
    ).not.toBe(403);
    expect(
      middleware(
        req("POST", { origin: "http://b.ts.net:3006", host: "b.ts.net:3006" })
      ).status
    ).not.toBe(403);
    // An origin NOT in the list is still rejected.
    expect(
      middleware(
        req("POST", { origin: "http://c.ts.net:3006", host: "c.ts.net:3006" })
      ).status
    ).toBe(403);
  });

  it("a present-but-empty Origin still 403s even with the extension set", () => {
    // The R17 no-fallthrough invariant must survive the opt-in widening.
    process.env.POLYECHO_WEB_ALLOWED_ORIGINS =
      "http://mac-mini.tail3e1efb.ts.net:3006";
    const res = middleware(
      req("POST", { origin: "", host: "mac-mini.tail3e1efb.ts.net:3006" })
    );
    expect(res.status).toBe(403);
  });

  it("ALLOWS localhost:3006 when POLYECHO_WEB_PORT=3006 (compose published port)", () => {
    process.env.POLYECHO_WEB_PORT = "3006";
    const ok = middleware(
      req("POST", { origin: "http://localhost:3006", host: "localhost:3006" })
    );
    expect(ok.status).not.toBe(403);
    const ok127 = middleware(
      req("POST", { origin: "http://127.0.0.1:3006", host: "127.0.0.1:3006" })
    );
    expect(ok127.status).not.toBe(403);
    // The container's internal listen port (3000) is NOT auto-allowed.
    const bad = middleware(
      req("POST", { origin: "http://localhost:3000", host: "localhost:3000" })
    );
    expect(bad.status).toBe(403);
  });
});
