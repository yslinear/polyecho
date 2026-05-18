/*
 * PolyEcho service worker — Web Push only (PRD §5.8, Phase 5).
 *
 * Deliberately NOT a caching service worker: the dashboard binds to
 * 127.0.0.1 and is always online when used. Offline/precaching would add
 * stale-data risk to a money instrument for zero benefit, so this worker
 * does exactly three things:
 *
 *   install         -> skipWaiting() so a new push contract activates fast
 *   activate        -> clients.claim() so open tabs get the new worker
 *   push            -> showNotification() from the JSON the push sender posts
 *   notificationclick-> focus an existing window (same origin) or open data.url
 *
 * The push payload shape mirrors the `events.payload.push` jsonb the Python
 * push sender emits: { title, body, url?, tag? }. Everything is defensive
 * because a malformed/empty push must never throw inside waitUntil.
 *
 * Handler bodies are also exported on self for unit testing in jsdom
 * (see __tests__/app/sw.test.ts) — production install/activate/push paths
 * are unaffected when `self.__swTestHandlers` is read by a test only.
 */

"use strict";

const ICON = "/icons/icon-192.png";
const BADGE = "/icons/icon-192.png";

/** Parse a PushEvent's data into a safe notification descriptor. */
function parsePush(event) {
  let data = {};
  try {
    if (event && event.data) {
      data = event.data.json() || {};
    }
  } catch (_e) {
    // Non-JSON or empty push: fall back to a generic alert.
    data = {};
  }
  const title =
    typeof data.title === "string" && data.title.trim() !== ""
      ? data.title
      : "PolyEcho";
  const body =
    typeof data.body === "string" ? data.body : "有新的跟單警示。";
  const url = typeof data.url === "string" && data.url ? data.url : "/alerts";
  const tag = typeof data.tag === "string" ? data.tag : undefined;
  return {
    title,
    options: {
      body,
      icon: ICON,
      badge: BADGE,
      tag,
      data: { url },
    },
  };
}

/** Focus an existing same-origin client, else open data.url (default /alerts). */
async function handleNotificationClick(notification, clientsApi, originUrl) {
  if (notification && typeof notification.close === "function") {
    notification.close();
  }
  const data = (notification && notification.data) || {};
  const rawTarget =
    typeof data.url === "string" && data.url ? data.url : "/alerts";

  // rev-web R18 I1: `client.navigate()` SILENTLY fails on Chrome when given
  // a RELATIVE path — it requires an ABSOLUTE URL. Resolve `rawTarget`
  // against the worker's own origin so navigation actually happens. We
  // resolve relative to `originUrl` (== self.location.origin) and, if the
  // result is somehow cross-origin (defensive — data.url is our own push
  // payload), fall back to the safe in-app default.
  let absoluteTarget;
  try {
    const u = new URL(rawTarget, originUrl);
    absoluteTarget =
      originUrl && u.origin === originUrl
        ? u.href
        : new URL("/alerts", originUrl).href;
  } catch (_e) {
    absoluteTarget = originUrl ? new URL("/alerts", originUrl).href : "/alerts";
  }

  const all = await clientsApi.matchAll({
    type: "window",
    includeUncontrolled: true,
  });
  for (const client of all) {
    // Same-origin tab already open: focus it (don't spawn duplicates).
    if (client.url && client.url.startsWith(originUrl) && "focus" in client) {
      try {
        // Compare against the resolved ABSOLUTE target (client.url is also
        // absolute) so we only navigate when actually changing location.
        if ("navigate" in client && client.url !== absoluteTarget) {
          await client.navigate(absoluteTarget).catch(() => {});
        }
      } catch (_e) {
        /* navigate is best-effort */
      }
      return client.focus();
    }
  }
  if (clientsApi.openWindow) {
    // openWindow accepts a relative URL fine, but the absolute form is
    // unambiguous and consistent with the navigate() path above.
    return clientsApi.openWindow(absoluteTarget);
  }
  return undefined;
}

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  const { title, options } = parsePush(event);
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  const origin = self.location ? self.location.origin : "";
  event.waitUntil(
    handleNotificationClick(event.notification, self.clients, origin)
  );
});

// Pure handler bodies exposed for unit tests (no effect in the browser path).
self.__swTestHandlers = { parsePush, handleNotificationClick };
