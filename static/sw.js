const CACHE_NAME = 'philforge-shell-__ASSET_VERSION__';
const APP_SHELL = [
  '/manifest.webmanifest',
  '/favicon.ico?v=__ASSET_VERSION__',
  '/static/pwa-icons/favicon-16.png?v=__ASSET_VERSION__',
  '/static/pwa-icons/favicon-32.png?v=__ASSET_VERSION__',
  '/static/pwa-icons/apple-touch-icon.png?v=__ASSET_VERSION__',
  '/static/logo.png?v=__ASSET_VERSION__',
  '/static/logolight.png?v=__ASSET_VERSION__',
  '/static/pwa-icons/icon-192.png?v=__ASSET_VERSION__',
  '/static/pwa-icons/icon-512.png?v=__ASSET_VERSION__',
  '/static/pwa-icons/icon-maskable-192.png?v=__ASSET_VERSION__',
  '/static/pwa-icons/icon-maskable-512.png?v=__ASSET_VERSION__',
  '/static/pwa.js?v=__ASSET_VERSION__',
  '/static/study_lounge.css?v=__ASSET_VERSION__',
  '/static/study_lounge.js?v=__ASSET_VERSION__',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).catch(() => undefined)
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      // Authenticated HTML is user-specific. Never cache a login/dashboard
      // navigation or replay one person's shell after an auth transition.
      fetch(request, { cache: 'no-store' }).catch(() => new Response(
        '<!doctype html><meta name="viewport" content="width=device-width"><title>PhilForge offline</title><main style="font:16px system-ui;padding:32px;max-width:560px;margin:auto"><h1>PhilForge is offline</h1><p>Reconnect to the network and reload. Trading state continues on the server.</p></main>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } }
      ))
    );
    return;
  }

  // Only versioned public shell assets belong in Cache Storage. Authenticated
  // chart images and study assets use non-/api routes, so a broad same-origin
  // cache rule would leak them across logout/login on a shared browser.
  const publicShellAsset = url.pathname.startsWith('/static/') || [
    '/manifest.webmanifest', '/favicon.ico', '/apple-touch-icon.png', '/logo.jpg', '/logo.png'
  ].includes(url.pathname);
  if (!publicShellAsset) return;

  event.respondWith(
    caches.match(request).then((cached) => {
      const networkFetch = fetch(request)
        .then((response) => {
          if (response && response.status === 200) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone)).catch(() => undefined);
          }
          return response;
        })
        .catch(() => cached);
      return cached || networkFetch;
    })
  );
});
