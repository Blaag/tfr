const CACHE_NAME = "tfr-shell-web1-v71";
const NETWORK_TIMEOUT_MS = 5000;
const SHELL = [
  "/",
  "/app.mjs",
  "/bootstrap.mjs",
  "/command.mjs",
  "/combo.mjs",
  "/connection-lifecycle.mjs",
  "/diagnostics.mjs",
  "/event-details.mjs",
  "/effects-lab.mjs",
  "/history-notice.mjs",
  "/linkify.mjs",
  "/motion.mjs",
  "/pairing.mjs",
  "/paste.mjs",
  "/presentation.mjs",
  "/push.mjs",
  "/scroll-restoration.mjs",
  "/screen-clear.mjs",
  "/service-worker.mjs",
  "/swipe.mjs",
  "/text-runs.mjs",
  "/transcript-events.mjs",
  "/styles.css",
  "/manifest.webmanifest",
  "/icon.svg",
  "/icon-512.png",
  "/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)));
});

self.addEventListener("message", (event) => {
  if (event.origin !== self.location.origin) return;
  if (event.data?.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/") || url.pathname === "/ws") {
    return;
  }
  if (url.search) return;
  if (!SHELL.includes(url.pathname)) return;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), NETWORK_TIMEOUT_MS);
  event.respondWith(
    fetch(event.request, { signal: controller.signal })
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((response) => response || caches.match("/")))
      .finally(() => clearTimeout(timeout)),
  );
});

self.addEventListener("push", (event) => {
  let valid = false;
  let world = null;
  let reason = null;
  try {
    const value = event.data?.json();
    valid = value?.version === 1 && value?.type === "activity";
    if (
      valid && typeof value.world === "string" && value.world.length > 0 && value.world.length <= 100 &&
      ["any_activity", "page", "mention"].includes(value.reason)
    ) {
      world = value.world;
      reason = value.reason;
    }
  } catch {
    valid = false;
  }
  const labels = {
    any_activity: "New activity",
    page: "Page received",
    mention: "Your character name was mentioned",
  };
  event.waitUntil(
    self.registration.showNotification(world ? `TFR · ${world}` : "New activity", {
      body: world && reason ? labels[reason] : undefined,
      icon: "/icon-512.png",
      badge: "/icon.svg",
      tag: "tfr-activity",
      renotify: false,
      data: { url: "/", valid },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const existing = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (existing) {
      await existing.focus();
      return;
    }
    await self.clients.openWindow(event.notification.data?.url || "/");
  })());
});
