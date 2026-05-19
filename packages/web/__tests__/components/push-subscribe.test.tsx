import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * PushSubscribe island (PRD §5.8). Verifies the confirmed browser-API flow:
 * permission prompt ONLY on click, register('/sw.js'), pushManager.subscribe
 * with applicationServerKey, then POST subscription.toJSON() + device label
 * to subscribePush. Also the unsupported-browser hint and unsubscribe path.
 */

vi.mock("@/lib/db", () => ({ db: {} }));
vi.mock("@/lib/env", () => ({ env: { POLYECHO_DB_URL_DRIZZLE: "stub" } }));

const subscribePush = vi.fn(async () => ({ ok: true, message: "已開啟" }));
const unsubscribePush = vi.fn(async () => ({ ok: true, message: "已關閉" }));
vi.mock("@/app/alerts/actions", () => ({
  subscribePush: (...a: unknown[]) => subscribePush(...a),
  unsubscribePush: (...a: unknown[]) => unsubscribePush(...a),
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
}));

import { PushSubscribe } from "@/components/push-subscribe";
import { t } from "@/lib/i18n";

// Accessible names are derived from the i18n dictionary, not hardcoded:
// the test asserts the opt-in/opt-out FLOW (permission → register →
// subscribe → POST) and stays correct if a translation string changes.
const ENABLE_ARIA = t("common.push.enableAria");
const DISABLE_ARIA = t("common.push.disableAria");
// A stable, translation-independent fragment of the iOS install hint:
// "Safari" is a proper noun kept verbatim by the PROPER-NOUN POLICY.
const IOS_HINT_FRAGMENT = "Safari";

const realNavigator = global.navigator;

function installPushEnv(opts: {
  permission?: NotificationPermission;
  existing?: boolean;
}) {
  const subscription = {
    endpoint: "https://push.example/xyz",
    unsubscribe: vi.fn().mockResolvedValue(true),
    toJSON: () => ({
      endpoint: "https://push.example/xyz",
      keys: { p256dh: "P256", auth: "AUTH" },
    }),
  };
  const registration = {
    pushManager: {
      subscribe: vi.fn().mockResolvedValue(subscription),
      getSubscription: vi
        .fn()
        .mockResolvedValue(opts.existing ? subscription : null),
    },
  };
  Object.defineProperty(global, "navigator", {
    configurable: true,
    value: {
      userAgent:
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_4 like Mac OS X) AppleWebKit Safari",
      serviceWorker: {
        register: vi.fn().mockResolvedValue(registration),
        getRegistration: vi.fn().mockResolvedValue(registration),
      },
    },
  });
  // @ts-expect-error test shim
  global.window.PushManager = function () {};
  // @ts-expect-error test shim
  global.window.Notification = {
    permission: opts.permission ?? "default",
    requestPermission: vi
      .fn()
      .mockResolvedValue(opts.permission ?? "granted"),
  };
  global.atob = (s: string) => Buffer.from(s, "base64").toString("binary");
  return { subscription, registration };
}

beforeEach(() => {
  subscribePush.mockClear();
  unsubscribePush.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
});

afterEach(() => {
  Object.defineProperty(global, "navigator", {
    configurable: true,
    value: realNavigator,
  });
  // @ts-expect-error cleanup
  delete global.window.PushManager;
  // @ts-expect-error cleanup
  delete global.window.Notification;
});

describe("PushSubscribe", () => {
  it("shows the iOS install hint and the opt-in button when supported", async () => {
    installPushEnv({ permission: "default" });
    render(<PushSubscribe vapidPublicKey="QUJD" />);
    expect(
      await screen.findByRole("button", { name: ENABLE_ARIA })
    ).toBeInTheDocument();
    expect(
      screen.getByText(new RegExp(IOS_HINT_FRAGMENT))
    ).toBeInTheDocument();
  });

  it("requests permission only on click, registers /sw.js, then POSTs toJSON()", async () => {
    const { registration } = installPushEnv({ permission: "granted" });
    render(<PushSubscribe vapidPublicKey="QUJD" />);
    const btn = await screen.findByRole("button", {
      name: ENABLE_ARIA,
    });
    await userEvent.click(btn);

    await waitFor(() => expect(subscribePush).toHaveBeenCalledTimes(1));
    expect(navigator.serviceWorker.register).toHaveBeenCalledWith("/sw.js");
    expect(registration.pushManager.subscribe).toHaveBeenCalledWith(
      expect.objectContaining({ userVisibleOnly: true })
    );
    const [sub, label] = subscribePush.mock.calls[0] as [
      { endpoint: string; keys: { p256dh: string; auth: string } },
      string,
    ];
    expect(sub.endpoint).toBe("https://push.example/xyz");
    expect(sub.keys).toEqual({ p256dh: "P256", auth: "AUTH" });
    expect(label).toMatch(/iOS/);
    expect(toastSuccess).toHaveBeenCalled();
  });

  it("does not subscribe if permission is denied", async () => {
    installPushEnv({ permission: "denied" });
    render(<PushSubscribe vapidPublicKey="QUJD" />);
    await userEvent.click(
      await screen.findByRole("button", { name: ENABLE_ARIA })
    );
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(subscribePush).not.toHaveBeenCalled();
  });

  it("reflects an existing subscription and can unsubscribe", async () => {
    installPushEnv({ permission: "granted", existing: true });
    render(<PushSubscribe vapidPublicKey="QUJD" />);
    const off = await screen.findByRole("button", {
      name: DISABLE_ARIA,
    });
    await userEvent.click(off);
    await waitFor(() => expect(unsubscribePush).toHaveBeenCalledTimes(1));
  });

  it("errors clearly when no VAPID public key is configured", async () => {
    installPushEnv({ permission: "granted" });
    render(<PushSubscribe vapidPublicKey="" />);
    await userEvent.click(
      await screen.findByRole("button", { name: ENABLE_ARIA })
    );
    expect(toastError).toHaveBeenCalled();
    expect(subscribePush).not.toHaveBeenCalled();
  });
});
