self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(self.registration.showNotification(data.title || "Auto-Insta", {
    body: data.body || "Você recebeu uma atualização.",
    icon: "/static/favicon.svg",
    badge: "/static/favicon.svg",
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(openClients => {
    const existing = openClients.find(client => "focus" in client);
    return existing ? existing.focus() : clients.openWindow("/dashboard");
  }));
});
