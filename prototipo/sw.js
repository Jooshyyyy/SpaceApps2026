// ARES: service worker de la app instalable. Guarda la interfaz, las librerías, las capas y el mapa
// para que abra rápido y aguante cortes de red. Nunca guarda la API en vivo (EVA, reloj, bus).
const V = 'ares-v1';
const SHELL = ['app.html', 'mission.json', 'logo_ares.png', 'oled_logo.png', 'manifest.webmanifest',
  'vendor/leaflet/leaflet.js', 'vendor/leaflet/leaflet.css', 'vendor/three/build/three.module.js',
  'vendor/three/examples/jsm/controls/MapControls.js', 'vendor/three/examples/jsm/controls/OrbitControls.js',
  'slope.u8', 'radiation.u8', 'thermal.u8', 'elevation.bin'];
const FRESH = /^\/(app\.html|mission\.json|manifest\.webmanifest|[a-z]+\.u8|elevation\.bin)$/;  // red primero, caché si no hay red
const KEEP = /^\/(tiles|vendor|pov|icons)\/|^\/(logo_ares|oled_logo)\.png$/;                     // caché primero (no cambian)

self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {})))).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;
  if (KEEP.test(u.pathname)) {
    e.respondWith(caches.match(r).then(hit => hit || fetch(r).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(V).then(c => c.put(r, copy)); }
      return res;
    })));
  } else if (FRESH.test(u.pathname)) {
    e.respondWith(fetch(r).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(V).then(c => c.put(u.pathname.slice(1), copy)); }
      return res;
    }).catch(() => caches.match(u.pathname.slice(1))));
  }
});
