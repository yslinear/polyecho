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
});
