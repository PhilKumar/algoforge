const CACHE_NAME = 'philforge-shell-v7';
const APP_SHELL = [
  '/',
  '/charts-viewer',
  '/market-movers',
  '/study-lounge',
  '/manifest.webmanifest',
  '/favicon.ico?v=20260402-1',
  '/static/pwa-icons/favicon-16.png?v=20260402-1',
  '/static/pwa-icons/favicon-32.png?v=20260402-1',
  '/static/pwa-icons/apple-touch-icon.png?v=20260402-1',
  '/static/logo.png?v=20260327-2',
  '/static/logolight.png?v=20260328-1',
  '/static/pwa-icons/icon-192.png?v=20260402-1',
  '/static/pwa-icons/icon-512.png?v=20260402-1',
  '/static/pwa-icons/icon-maskable-192.png?v=20260402-1',
  '/static/pwa-icons/icon-maskable-512.png?v=20260402-1',
  '/static/pwa.js?v=20260401-4',
  '/static/study_lounge.css?v=20260401-3',
  '/static/study_lounge.js?v=20260401-1',
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
      fetch(request)
        .then((response) => {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone)).catch(() => undefined);
          return response;
        })
        .catch(async () => {
          const cached = await caches.match(request);
          return cached || caches.match('/');
        })
    );
    return;
  }

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
