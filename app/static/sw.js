// Minimal service worker: makes the app installable. Network-only — nothing
// is cached, so the UI is never stale.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
