const CACHE = 'rail-station-v6';
const FILES = ['./', './index.html', './stations.js', './tracks.js', './manifest.webmanifest', './icon-180.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// קודם מהמטמון (עובד בלי אינטרנט), ובמקביל מרענן ברקע כשיש רשת
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;
  const net = fetch(req).then((res) => {
    // תגובה שעברה הפניה (redirect) לא תקינה לניווט ב-Safari, לכן לא שומרים אותה
    if (res && res.ok && !res.redirected) {
      const copy = res.clone();
      return caches.open(CACHE).then((c) => c.put(req, copy)).then(() => res);
    }
    return res;
  });
  e.waitUntil(net.catch(() => {}));
  e.respondWith(
    caches.match(req, { ignoreSearch: true }).then((hit) =>
      hit || net.catch(() =>
        // בלי רשת ובלי עותק: פתיחת האפליקציה תמיד מחזירה את הדף הראשי
        req.mode === 'navigate' ? caches.match('./index.html') : Response.error()
      )
    )
  );
});
