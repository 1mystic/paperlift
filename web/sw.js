/* Hands /out/* previews, downloads and audit images to the page, which asks the in-browser engine. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  const scope = new URL(self.registration.scope).pathname;          // e.g. /paperlift/
  if (u.origin !== location.origin || !u.pathname.startsWith(scope)) return;
  const rel = u.pathname.slice(scope.length);
  if (!(rel.startsWith('out/') || rel === 'api/audit-page' || rel === 'api/zip')) return;
  e.respondWith(bridge(e, '/' + rel + u.search));
});

async function bridge(e, path) {
  const all = await self.clients.matchAll({type: 'window'});
  const client = all.find(c => c.url.includes('/app/')) || all[0];
  if (!client) return new Response('Open the Paperlift app first.', {status: 503});
  return new Promise((resolve) => {
    const ch = new MessageChannel();
    ch.port1.onmessage = (m) => resolve(new Response(m.data.body, {status: m.data.status, headers: m.data.headers}));
    client.postMessage({plreq: true, method: e.request.method, path}, [ch.port2]);
    setTimeout(() => resolve(new Response('Timed out', {status: 504})), 120000);
  });
}
