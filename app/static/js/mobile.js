(() => {
  const views = document.querySelectorAll("[data-view]");
  const navItems = document.querySelectorAll("[data-target]");
  function showView(target) {
    views.forEach(view => view.classList.toggle("active", view.dataset.view === target));
    navItems.forEach(item => item.classList.toggle("active", item.dataset.target === target));
    history.replaceState(null, "", `#${target}`);
  }
  navItems.forEach(item => item.addEventListener("click", () => showView(item.dataset.target)));
  showView(location.hash.slice(1) || "hub");

  async function enableNotifications() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return;
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return;
    const registration = await navigator.serviceWorker.register("/sw.js", {scope: "/"});
    const keyResponse = await fetch("/api/notifications/vapid-public-key");
    if (!keyResponse.ok) return;
    const {public_key: publicKey} = await keyResponse.json();
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    });
    await fetch("/api/notifications/subscribe", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(subscription.toJSON()),
    });
    document.getElementById("enable-notifications").textContent = "✅";
  }
  function urlBase64ToUint8Array(value) {
    const padding = "=".repeat((4 - value.length % 4) % 4);
    const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(base64), character => character.charCodeAt(0));
  }
  document.getElementById("enable-notifications")?.addEventListener("click", enableNotifications);
})();
