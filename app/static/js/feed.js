import { escapeHtml, showModuleToast } from "./dashboard.js";

function selectedIds() {
  return [...document.querySelectorAll("[data-feed-account]:checked")].map(input => Number(input.value));
}

function setupSelectableCards(cards, inputSelector) {
  let anchor = -1;
  let dragging = false;
  const sync = () => cards.forEach(card => {
    const input = card.querySelector(inputSelector);
    card.classList.toggle("selected", Boolean(input?.checked));
  });
  cards.forEach((card, index) => {
    card.classList.add("selectable-card");
    card.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      event.preventDefault();
      if (event.shiftKey && anchor >= 0) {
        const [start, end] = [anchor, index].sort((a, b) => a - b);
        cards.forEach((item, itemIndex) => { item.querySelector(inputSelector).checked = itemIndex >= start && itemIndex <= end; });
      } else if (event.ctrlKey || event.metaKey) {
        const input = card.querySelector(inputSelector);
        input.checked = !input.checked;
        anchor = index;
      } else {
        cards.forEach(item => { item.querySelector(inputSelector).checked = false; });
        card.querySelector(inputSelector).checked = true;
        anchor = index;
      }
      dragging = true;
      sync();
    });
    card.addEventListener("pointerenter", event => {
      if (!dragging || event.buttons !== 1) return;
      card.querySelector(inputSelector).checked = true;
      sync();
    });
  });
  document.addEventListener("pointerup", () => { dragging = false; });
  sync();
}

export function initFeedModule() {
  const grid = document.getElementById("feed-account-cards");
  const loadButton = document.getElementById("feed-load-modular");
  if (!grid || !loadButton) return;
  setupSelectableCards([...grid.querySelectorAll(".feed-account-card")], "[data-feed-account]");
  grid.addEventListener("change", event => {
    const card = event.target.closest(".feed-account-card");
    if (card) {
      card.classList.toggle("selected", event.target.checked);
      const legacySelect = document.getElementById("feed-accounts");
      const option = legacySelect?.querySelector(`option[value="${event.target.value}"]`);
      if (option) option.selected = event.target.checked;
    }
  });
  document.getElementById("feed-select-all")?.addEventListener("click", () => {
    grid.querySelectorAll("[data-feed-account]").forEach(input => {
      input.checked = true;
      input.closest(".feed-account-card").classList.add("selected");
      const option = document.getElementById("feed-accounts")?.querySelector(`option[value="${input.value}"]`);
      if (option) option.selected = true;
    });
  });
  document.getElementById("feed-clear-selection")?.addEventListener("click", () => {
    grid.querySelectorAll("[data-feed-account]").forEach(input => {
      input.checked = false;
      input.closest(".feed-account-card").classList.remove("selected");
      const option = document.getElementById("feed-accounts")?.querySelector(`option[value="${input.value}"]`);
      if (option) option.selected = false;
    });
  });
  let feedItems = [];
  const renderFeed = () => {
    const feedGrid = document.getElementById("feed-grid");
    feedGrid.innerHTML = feedItems.map((item, index) => `
      <article class="feed-item">
        <input type="checkbox" data-feed-index="${index}" aria-label="Selecionar publicação">
        <img src="${escapeHtml(item.thumbnail_url || item.media_url || "")}" alt="Publicação de @${escapeHtml(item.account || "")}">
        <div class="feed-item-body"><strong>@${escapeHtml(item.account || "")}</strong><small>${escapeHtml(item.caption || item.media_type || "Publicação")}</small><div class="feed-metrics"><span title="Curtidas">♥ ${Number(item.likes || item.like_count || 0).toLocaleString("pt-BR")}</span><span title="Impressões">◉ ${Number(item.insights?.impressions || 0).toLocaleString("pt-BR")}</span><span title="Alcance">↗ ${Number(item.insights?.reach || 0).toLocaleString("pt-BR")}</span>${item.insights?.plays ? `<span title="Reproduções">▶ ${Number(item.insights.plays).toLocaleString("pt-BR")}</span>` : ""}</div></div>
      </article>`).join("") || "<p class=\"muted\">Nenhuma publicação encontrada.</p>";
    setupSelectableCards([...feedGrid.querySelectorAll(".feed-item")], "[data-feed-index]");
  };
  const load = async () => {
    const ids = selectedIds();
    if (!ids.length) return showModuleToast("Selecione ao menos uma conta.", "error");
    const response = await fetch(`/api/feed?${ids.map(id => `account_ids=${id}`).join("&")}`, { cache: "no-store" });
    const payload = await response.json();
    const status = document.getElementById("feed-status");
    if (!response.ok) return showModuleToast(payload.detail || "Falha ao carregar o Feed.", "error");
    status.textContent = `${payload.items?.length || 0} publicação(ões) carregada(s).`;
    feedItems = payload.items || [];
    renderFeed();
  };
  loadButton.addEventListener("click", load);
  document.getElementById("feed-load")?.addEventListener("click", load);
  async function deleteFeed(clearFeed) {
    const ids = selectedIds();
    const selected = [...document.querySelectorAll("[data-feed-index]:checked")].map(input => {
      const item = feedItems[Number(input.dataset.feedIndex)];
      return { account_id: item.account_id, media_id: item.id };
    });
    if (!ids.length) return showModuleToast("Selecione ao menos uma conta.", "error");
    if (!clearFeed && !selected.length) return showModuleToast("Selecione ao menos uma publicação.", "error");
    if (!confirm(clearFeed ? "Apagar todas as publicações das contas selecionadas?" : "Apagar as publicações selecionadas?")) return;
    const response = await fetch("/api/feed/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_ids: ids, media: selected, clear_feed: clearFeed }),
    });
    const payload = await response.json();
    if (!response.ok) return showModuleToast(payload.detail || "Falha ao apagar publicações.", "error");
    showModuleToast(`${payload.deleted?.length || 0} publicação(ões) apagada(s).`, payload.errors?.length ? "error" : "success");
    load();
  }
  document.getElementById("feed-delete-selected")?.addEventListener("click", () => deleteFeed(false));
  document.getElementById("feed-clear")?.addEventListener("click", () => deleteFeed(true));
}

initFeedModule();
