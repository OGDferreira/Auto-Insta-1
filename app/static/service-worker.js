const CACHE_NAME = "auto-insta-shell-v2";
const APP_SHELL = ["/static/favicon.svg", "/static/manifest.webmanifest"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET" || new URL(event.request.url).origin !== self.location.origin) return;
  const requestUrl = new URL(event.request.url);
  if (requestUrl.pathname !== "/static/favicon.svg" && requestUrl.pathname !== "/static/manifest.webmanifest") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});

self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(self.registration.showNotification(data.title || "Auto-Insta", {
    body: data.body || "Você recebeu uma atualização.",
    icon: "/static/favicon.svg",
    badge: "/static/favicon.svg",
    data: {url: data.url || "/dashboard"},
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || "/dashboard";
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(openClients => {
    const existing = openClients.find(client => "focus" in client);
    if (existing) {
      existing.navigate(targetUrl);
      return existing.focus();
    }
    return clients.openWindow(targetUrl);
  }));
});
