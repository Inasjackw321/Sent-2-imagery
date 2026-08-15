// Service worker: keeps the app shell available so the window opens instantly
// (and offline). Imagery always comes from the network -- never cached.

// Bumped whenever the shell changes, so an installed copy picks the new one up.
const VERSION = 'sent2-v4';
const SHELL = [
  '/',
  '/css/app.css',
  '/js/main.js',
  '/js/api.js',
  '/js/store.js',
  '/js/ui.js',
  '/js/imagery.js',
  '/js/adjust.js',
  '/js/map.js',
  '/js/install.js',
  '/vendor/leaflet/leaflet.js',
  '/vendor/leaflet/leaflet.css',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      // Don't let one missing file abort the whole install.
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;      // tiles, geocoding: network
  if (url.pathname.startsWith('/api/')) return;         // imagery: always fresh

  // Stale-while-revalidate: instant start, but edits to the app still land.
  event.respondWith(
    caches.open(VERSION).then(async (cache) => {
      const cached = await cache.match(request);
      const network = fetch(request)
        .then((response) => {
          if (response.ok) cache.put(request, response.clone());
          return response;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});
