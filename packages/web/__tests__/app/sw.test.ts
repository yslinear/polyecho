import { describe, it, expect, vi, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

/**
 * public/sw.js — service worker push + notificationclick handlers.
 *
 * The SW runs in a ServiceWorkerGlobalScope, not jsdom. We evaluate the file
 * in a node:vm with a minimal `self` stub, capture addEventListener handlers,
 * and unit-test the pure logic exposed on self.__swTestHandlers:
 *
 *  - parsePush: JSON push -> showNotification descriptor; empty/garbage push
 *    falls back to a generic on-brand alert (never throws inside waitUntil).
 *  - handleNotificationClick: closes the notification, focuses an existing
 *    same-origin client, else openWindow(data.url || "/alerts").
 */

interface SwHandlers {
  parsePush: (event: { data?: { json: () => unknown } }) => {
    title: string;
    options: {
      body: string;
      icon: string;
      badge: string;
      data: { url: string };
    };
  };
  handleNotificationClick: (
    notification: {
      close: () => void;
      data?: { url?: string };
    } | null,
    clientsApi: {
      matchAll: (o: unknown) => Promise<unknown[]>;
      openWindow?: (u: string) => Promise<unknown>;
    },
    origin: string
  ) => Promise<unknown>;
}

let handlers: SwHandlers;
let registeredEvents: Record<string, (e: unknown) => void>;

beforeAll(() => {
  const code = readFileSync(
    resolve(__dirname, "../../public/sw.js"),
    "utf8"
  );
  registeredEvents = {};
  const self: Record<string, unknown> = {
    addEventListener: (type: string, fn: (e: unknown) => void) => {
      registeredEvents[type] = fn;
    },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(), matchAll: vi.fn(), openWindow: vi.fn() },
    registration: { showNotification: vi.fn() },
    location: { origin: "http://localhost:3000" },
  };
  const ctx = vm.createContext({ self, atob, console });
  vm.runInContext(code, ctx);
  handlers = (self.__swTestHandlers as SwHandlers);
});

describe("sw.js registration", () => {
  it("registers install / activate / push / notificationclick", () => {
    expect(Object.keys(registeredEvents).sort()).toEqual(
      ["activate", "install", "notificationclick", "push"].sort()
    );
  });
});

describe("parsePush", () => {
  it("maps a JSON push to a notification descriptor", () => {
    const out = handlers.parsePush({
      data: {
        json: () => ({
          title: "停損觸發",
          body: "0xabc -$120",
          url: "/positions",
        }),
      },
    });
    expect(out.title).toBe("停損觸發");
    expect(out.options.body).toBe("0xabc -$120");
    expect(out.options.icon).toBe("/icons/icon-192.png");
    expect(out.options.badge).toBe("/icons/icon-192.png");
    expect(out.options.data.url).toBe("/positions");
  });

  it("falls back to a generic alert for an empty/garbage push", () => {
    const out = handlers.parsePush({
      data: {
        json: () => {
          throw new Error("not json");
        },
      },
    });
    expect(out.title).toBe("PolyEcho");
    expect(out.options.data.url).toBe("/alerts");
  });

  it("defaults the url to /alerts when missing", () => {
    const out = handlers.parsePush({ data: { json: () => ({ body: "x" }) } });
    expect(out.options.data.url).toBe("/alerts");
  });
});

describe("handleNotificationClick", () => {
  it("focuses an existing same-origin client instead of opening a new one", async () => {
    const close = vi.fn();
    const focus = vi.fn().mockResolvedValue("focused");
    const navigate = vi.fn().mockResolvedValue(undefined);
    const client = {
      url: "http://localhost:3000/",
      focus,
      navigate,
    };
    const clientsApi = {
      matchAll: vi.fn().mockResolvedValue([client]),
      openWindow: vi.fn(),
    };
    await handlers.handleNotificationClick(
      { close, data: { url: "/alerts" } },
      clientsApi,
      "http://localhost:3000"
    );
    expect(close).toHaveBeenCalled();
    expect(focus).toHaveBeenCalled();
    expect(clientsApi.openWindow).not.toHaveBeenCalled();
  });

  it("opens a new window when no client is open (default /alerts)", async () => {
    const close = vi.fn();
    const openWindow = vi.fn().mockResolvedValue("opened");
    const clientsApi = {
      matchAll: vi.fn().mockResolvedValue([]),
      openWindow,
    };
    await handlers.handleNotificationClick(
      { close, data: {} },
      clientsApi,
      "http://localhost:3000"
    );
    expect(close).toHaveBeenCalled();
    expect(openWindow).toHaveBeenCalledWith("/alerts");
  });

  it("opens the payload url when provided and no client matches", async () => {
    const openWindow = vi.fn().mockResolvedValue("opened");
    await handlers.handleNotificationClick(
      { close: vi.fn(), data: { url: "/positions" } },
      { matchAll: vi.fn().mockResolvedValue([]), openWindow },
      "http://localhost:3000"
    );
    expect(openWindow).toHaveBeenCalledWith("/positions");
  });
});
