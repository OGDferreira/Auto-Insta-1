export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[char]));
}

export function showModuleToast(message, type = "info") {
  if (typeof window.showToast === "function") {
    window.showToast(message, type);
  } else {
    console[type === "error" ? "error" : "log"](message);
  }
}

export function initDashboardModules() {
  document.querySelectorAll("[data-module-view]").forEach(link => {
    link.addEventListener("click", event => {
      event.preventDefault();
      const view = link.dataset.moduleView;
      document.querySelectorAll("[data-panel]").forEach(panel => {
        panel.classList.toggle("active", panel.dataset.panel === view);
      });
      document.querySelectorAll("[data-module-view]").forEach(item => item.classList.toggle("active", item === link));
      history.replaceState(null, "", `${location.pathname}${location.search}#${view}`);
    });
  });
}
