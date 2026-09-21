export function initQueueModule() {
  document.querySelectorAll("[data-calendar-status]").forEach(item => {
    item.classList.toggle("status-badge", true);
  });
}
