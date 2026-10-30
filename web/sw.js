/* Static-asset cache, network-first: the network always wins while it is
 * reachable, so a deploy can never serve a stale shell/settings/vendor file;
 * the cache only saves a repeat visit (or a dead connection) from re-fetching
 * them. Pages, /api/ and /ws/ are never cached here — the server already
 * sends them no-store, and transcripts/login state must never come from cache.
 * Bump CACHE_NAME when the caching rules themselves change; asset content
 * changes need no bump, because the network is tried first on every load. */
const CACHE_NAME = 'tmux-chat-shell-v2';
const STATIC_PREFIXES = [
  '/assets/shell.', '/assets/settings.js', '/assets/app-icon',
  '/assets/xterm/', '/assets/mermaid/', '/assets/ansi_up.js', '/assets/marked.esm.js',
];
const isStatic = pathname =>
  pathname === '/manifest.webmanifest' || STATIC_PREFIXES.some(prefix => pathname.startsWith(prefix));

self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
    .then(() => self.clients.claim())
));
self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/') || url.pathname === '/' || url.pathname === '/login') {
    event.respondWith(fetch(request, {cache: 'no-store'}));
    return;
  }
  if (!isStatic(url.pathname)) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.ok) {
        const cache = await caches.open(CACHE_NAME);
        cache.put(request, response.clone()).catch(() => {});
      }
      return response;
    } catch (error) {
      const cached = await caches.match(request);
      if (cached) return cached;
      throw error;
    }
  })());
});
