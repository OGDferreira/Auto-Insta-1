import { escapeHtml, showModuleToast } from "./dashboard.js";

function selectedIds() {
  return [...document.querySelectorAll("[data-feed-account]:checked")].map(input => Number(input.value));
}

export function initFeedModule() {
  const grid = document.getElementById("feed-account-cards");
  const loadButton = document.getElementById("feed-load-modular");
  if (!grid || !loadButton) return;
  grid.addEventListener("change", event => {
    const card = event.target.closest(".feed-account-card");
    if (card) {
      card.classList.toggle("selected", event.target.checked);
      const legacySelect = document.getElementById("feed-accounts");
      const option = legacySelect?.querySelector(`option[value="${event.target.value}"]`);
      if (option) option.selected = event.target.checked;
    }
  });
  loadButton.addEventListener("click", async () => {
    const ids = selectedIds();
    if (!ids.length) return showModuleToast("Selecione ao menos uma conta.", "error");
    const response = await fetch(`/api/feed?${ids.map(id => `account_ids=${id}`).join("&")}`, { cache: "no-store" });
    const payload = await response.json();
    const status = document.getElementById("feed-status");
    if (!response.ok) return showModuleToast(payload.detail || "Falha ao carregar o Feed.", "error");
    status.textContent = `${payload.items?.length || 0} publicação(ões) carregada(s).`;
    const feedGrid = document.getElementById("feed-grid");
    feedGrid.innerHTML = (payload.items || []).map((item, index) => `
      <article class="feed-item">
        <input type="checkbox" data-feed-index="${index}" aria-label="Selecionar publicação">
        <img src="${escapeHtml(item.thumbnail_url || item.media_url || "")}" alt="Publicação de @${escapeHtml(item.account || "")}">
        <div class="feed-item-body"><strong>@${escapeHtml(item.account || "")}</strong><small>${escapeHtml(item.caption || item.media_type || "Publicação")}</small></div>
      </article>`).join("") || "<p class=\"muted\">Nenhuma publicação encontrada.</p>";
  });
}

initFeedModule();
