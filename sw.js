const CACHE_NAME = "auto-insta-v2";
const APP_SHELL = ["/dashboard", "/static/manifest.webmanifest", "/static/favicon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
  )));
  self.clients.claim();
});
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {body: event.data?.text()}; }
  event.waitUntil(self.registration.showNotification(data.title || "Auto-Insta", {
    body: data.body || "Você recebeu uma atualização.",
    icon: "/static/favicon.svg",
    badge: "/static/favicon.svg",
    data: {url: data.url || "/dashboard"},
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = event.notification.data?.url || "/dashboard";
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(openClients => {
    const existing = openClients.find(client => "focus" in client);
    return existing ? existing.navigate(target).then(() => existing.focus()) : clients.openWindow(target);
  }));
});
