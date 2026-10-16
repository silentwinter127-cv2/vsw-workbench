/* 手机版 service worker：把外壳缓存住，让「添加到主屏幕」后即使连不上电脑也能打开。
 *
 * 只缓存外壳（HTML/manifest/图标），**不缓存 API 与图片**：
 *   - 快照数据由手机自己的 IndexedDB 管（见 app 里的 localdb），离线读它；
 *   - 图片按需取一次就存进 IndexedDB，而不是靠 HTTP 缓存，这样离线也能看。
 * 这样就不会出现"离线时 API 被缓存住、以为拿到了新数据"这种坑。
 */
const SHELL = 'vsw-mobile-shell-v1';
const SHELL_FILES = ['./', './index.html', './manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  // 只接管同源的外壳请求；API、媒体、跨源一律直连（失败了就让它失败，不要拿缓存糊弄）
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/')) return;

  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(SHELL).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        })
        .catch(() => cached || caches.match('./index.html'));
      return cached || network;
    })
  );
});
