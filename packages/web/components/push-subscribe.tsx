"use client";

/**
 * PushSubscribe — Web Push opt-in island (PRD §5.8).
 *
 * Confirmed browser-API pattern (context7):
 *   navigator.serviceWorker.register('/sw.js')
 *   -> registration.pushManager.subscribe({ userVisibleOnly:true,
 *        applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC) })
 *   -> POST subscription.toJSON() ({endpoint,keys:{p256dh,auth}}) + device
 *      label to the subscribePush Server Action (UPSERTs on UNIQUE endpoint).
 *
 * Notification.permission is requested ONLY on an explicit button click.
 * The VAPID PUBLIC key arrives as a prop from a Server Component reading the
 * `vapid_public_key` settings row — the private key is never exposed.
 *
 * iOS Safari 16.4+ only delivers Web Push when the app is installed to the
 * Home Screen, so we surface that hint plainly. No modal — a calm inline
 * panel matching the /settings register.
 */

import * as React from "react";
import { Bell, BellOff, Info } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { subscribePush, unsubscribePush } from "@/app/alerts/actions";
import { t } from "@/lib/i18n";

/**
 * base64url VAPID key -> Uint8Array for applicationServerKey.
 *
 * Backed by an explicit ArrayBuffer (not ArrayBufferLike) so the type
 * matches the DOM `PushSubscriptionOptionsInit.applicationServerKey`
 * (BufferSource) under TS 5.7's stricter typed-array generics.
 */
function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding)
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  const raw = atob(base64);
  const buffer = new ArrayBuffer(raw.length);
  const out = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** Compact device label from the UA (best effort, never PII-heavy). */
function deviceLabelFromUA(): string {
  if (typeof navigator === "undefined") return t("common.push.unknownDevice");
  const ua = navigator.userAgent;
  const os = /iPhone|iPad|iPod/.test(ua)
    ? "iOS"
    : /Android/.test(ua)
      ? "Android"
      : /Mac/.test(ua)
        ? "macOS"
        : /Windows/.test(ua)
          ? "Windows"
          : t("common.push.otherOs");
  const browser = /CriOS|Chrome/.test(ua)
    ? "Chrome"
    : /Firefox/.test(ua)
      ? "Firefox"
      : /Safari/.test(ua)
        ? "Safari"
        : t("common.push.otherBrowser");
  return `${os} ${browser}`;
}

type State = "idle" | "working" | "subscribed" | "unsupported";

export function PushSubscribe({ vapidPublicKey }: { vapidPublicKey: string }) {
  const [state, setState] = React.useState<State>("idle");
  const [endpoint, setEndpoint] = React.useState<string | null>(null);

  const supported = React.useMemo(
    () =>
      typeof window !== "undefined" &&
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window,
    []
  );

  // Reflect an already-active subscription on mount (no permission prompt).
  React.useEffect(() => {
    if (!supported) {
      setState("unsupported");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const reg = await navigator.serviceWorker.getRegistration();
        const existing = await reg?.pushManager.getSubscription();
        if (!cancelled && existing) {
          setEndpoint(existing.endpoint);
          setState("subscribed");
        }
      } catch {
        /* non-fatal: leave state idle */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [supported]);

  async function onSubscribe() {
    if (!vapidPublicKey) {
      toast.error(t("common.push.errNoVapid"));
      return;
    }
    setState("working");
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        toast.error(t("common.push.errPermission"));
        setState("idle");
        return;
      }
      const reg = await navigator.serviceWorker.register("/sw.js");
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
      });
      const json = sub.toJSON() as {
        endpoint?: string;
        keys?: { p256dh?: string; auth?: string };
      };
      const res = await subscribePush(
        {
          endpoint: json.endpoint ?? "",
          keys: {
            p256dh: json.keys?.p256dh ?? "",
            auth: json.keys?.auth ?? "",
          },
        },
        deviceLabelFromUA()
      );
      if (res.ok) {
        setEndpoint(sub.endpoint);
        setState("subscribed");
        toast.success(res.message);
      } else {
        setState("idle");
        toast.error(res.message);
      }
    } catch {
      setState("idle");
      toast.error(t("common.push.errSubscribe"));
    }
  }

  async function onUnsubscribe() {
    setState("working");
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = await reg?.pushManager.getSubscription();
      const ep = sub?.endpoint ?? endpoint;
      if (sub) await sub.unsubscribe();
      if (ep) await unsubscribePush(ep);
      setEndpoint(null);
      setState("idle");
      toast.success(t("common.push.okUnsubscribe"));
    } catch {
      setState("subscribed");
      toast.error(t("common.push.errUnsubscribe"));
    }
  }

  if (state === "unsupported") {
    return (
      <p className="max-w-prose text-sm text-muted-foreground">
        {t("common.push.unsupported")}
      </p>
    );
  }

  const isSubscribed = state === "subscribed";
  const busy = state === "working";

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        {isSubscribed ? (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={onUnsubscribe}
            aria-label={t("common.push.disableAria")}
          >
            <BellOff className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
            {busy ? t("common.push.working") : t("common.push.disable")}
          </Button>
        ) : (
          <Button
            size="sm"
            disabled={busy}
            onClick={onSubscribe}
            aria-label={t("common.push.enableAria")}
          >
            <Bell className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />
            {busy ? t("common.push.working") : t("common.push.enable")}
          </Button>
        )}
        <span
          className="text-xs text-muted-foreground"
          aria-live="polite"
          role="status"
        >
          {isSubscribed
            ? t("common.push.subscribed")
            : t("common.push.notSubscribed")}
        </span>
      </div>

      <p className="flex max-w-prose items-start gap-1.5 text-xs text-muted-foreground">
        <Info
          className="mt-0.5 h-3.5 w-3.5 shrink-0"
          aria-hidden="true"
        />
        <span>{t("common.push.iosHint")}</span>
      </p>
    </div>
  );
}
