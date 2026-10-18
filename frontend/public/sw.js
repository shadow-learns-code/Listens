/* Shadow Listens service worker.
 *
 * Deliberately small and conservative. It makes the app open instantly and show a friendly page when offline, and it
 * NEVER touches anything that must stay live:
 *   - /api/*            data, sign-in, signed stream links: always the network
 *   - other origins     audio segments and covers come from the CDN Worker: always the network
 *   - Range requests    audio seeking
 * What it does handle:
 *   - /_next/static/*, /icons/*   content-hashed or fixed assets: cache first (safe, they never change in place)
 *   - page navigations            the page shells are client-rendered (no private data in the HTML), so a cached shell is
 *                                  shown if the network has not answered within 2.5 s: a sleeping free-tier server takes
 *                                  ~25 s to wake, and the app's own "waking up" banner then explains the wait
 */
const VERSION = "v1";
const STATIC = `listens-static-${VERSION}`;
const PAGES = `listens-pages-${VERSION}`;
const SHELL = ["/offline.html", "/icons/icon-192.png", "/icons/icon-512.png"];
const MAX_PAGES = 40;
const NETWORK_WAIT_MS = 2500;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(STATIC).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys()) if (key !== STATIC && key !== PAGES) await caches.delete(key);
      await self.clients.claim();
    })(),
  );
});

async function cacheFirst(request) {
  const cache = await caches.open(STATIC);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone());
  return response;
}

async function trimPages(cache) {
  const keys = await cache.keys();
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_PAGES))) await cache.delete(key);
}

async function page(event) {
  const request = event.request;
  const cache = await caches.open(PAGES);
  const cached = await cache.match(request);
  const network = fetch(request).then(async (response) => {
    if (response.ok && response.type === "basic") {
      await cache.put(request, response.clone());
      await trimPages(cache);
    }
    return response;
  });
  if (!cached) {
    try {
      return await network;
    } catch {
      return (await caches.match("/offline.html")) || Response.error();
    }
  }
  event.waitUntil(network.catch(() => undefined)); // keep refreshing the cached shell even after the race below is decided
  return Promise.race([network.catch(() => cached), new Promise((resolve) => setTimeout(() => resolve(cached), NETWORK_WAIT_MS))]);
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET" || request.headers.has("range")) return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/icons/")) {
    event.respondWith(cacheFirst(request));
  } else if (request.mode === "navigate") {
    event.respondWith(page(event));
  }
});
