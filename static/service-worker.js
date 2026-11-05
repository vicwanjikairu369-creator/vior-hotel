const CACHE_NAME = 'vior-shell-v1';
const APP_SHELL = [
    '/static/css/style.css',
    '/static/js/script.js',
    '/static/images/app-icon.svg',
    '/static/offline.html',
];

self.addEventListener('install', event => {
    event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys => Promise.all(
            keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)),
        )),
    );
    self.clients.claim();
});

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/account') || url.pathname.startsWith('/admin')) return;
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
        return;
    }
    if (event.request.mode === 'navigate') {
        event.respondWith(fetch(event.request).catch(() => caches.match('/static/offline.html')));
    }
});
