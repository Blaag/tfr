const CACHE_NAME = "tfr-shell-web1-v46";
const SHELL = [
  "/",
  "/app.mjs",
  "/command.mjs",
  "/combo.mjs",
  "/connection-lifecycle.mjs",
  "/event-details.mjs",
  "/history-notice.mjs",
  "/linkify.mjs",
  "/motion.mjs",
  "/pairing.mjs",
  "/presentation.mjs",
  "/swipe.mjs",
  "/text-runs.mjs",
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
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((response) => response || caches.match("/"))),
  );
});
