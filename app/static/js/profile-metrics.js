const profileButtons = [...document.querySelectorAll("[data-metrics-account]")];
const profileList = document.getElementById("metrics-profile-list");
const profileFeed = document.getElementById("profile-feed");
const statusMessage = document.getElementById("profile-metrics-status");
const feedCount = document.getElementById("profile-feed-count");
const selectedName = document.getElementById("selected-profile-name");
const refreshButton = document.getElementById("profile-metrics-refresh");
const engagementSort = document.getElementById("profile-engagement-sort");
const profileCache = new Map();
let selectedAccountId = null;
let requestVersion = 0;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function formatCount(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("pt-BR") : "—";
}

function setMetric(name, value) {
  const target = document.querySelector(`[data-profile-stat="${name}"]`);
  if (target) target.textContent = formatCount(value);
}

function showMessage(message, isError = false) {
  profileFeed.innerHTML = `<div class="profile-feed-message${isError ? " error" : ""}">${escapeHtml(message)}</div>`;
  feedCount.textContent = "";
}

async function fetchAnalytics(query) {
  const response = await fetch(`/api/analytics?${query}`, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Não foi possível carregar as métricas.");
  return payload;
}

function updateProfileRows(accounts) {
  accounts.forEach(account => {
    profileCache.set(String(account.account_id), account);
    const button = profileButtons.find(item => item.dataset.metricsAccount === String(account.account_id));
    const followerNode = button?.querySelector("[data-account-followers]");
    if (followerNode) followerNode.textContent = `${formatCount(account.followers)} seguidores`;
  });
}

function renderMedia(items) {
  if (!items.length) {
    showMessage("Nenhuma mídia recente disponível para este perfil.");
    return;
  }
  feedCount.textContent = `${items.length.toLocaleString("pt-BR")} mídias`;
  const sortedItems = items.map((item, index) => ({ item, index })).sort((left, right) => {
    const leftValue = Number(left.item.engagement);
    const rightValue = Number(right.item.engagement);
    const leftMissing = left.item.engagement === null || left.item.engagement === undefined || !Number.isFinite(leftValue);
    const rightMissing = right.item.engagement === null || right.item.engagement === undefined || !Number.isFinite(rightValue);
    if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
    if (leftMissing) return left.index - right.index;
    const difference = engagementSort?.value === "asc" ? leftValue - rightValue : rightValue - leftValue;
    return difference || left.index - right.index;
  }).map(entry => entry.item);
  profileFeed.innerHTML = sortedItems.map(item => {
    const video = String(item.media_type || "").toUpperCase().includes("VIDEO");
    const source = escapeHtml(item.thumbnail_url || item.media_url || "");
    const mediaMarkup = video && !item.thumbnail_url
      ? `<video src="${source}" muted playsinline preload="metadata"></video>`
      : `<img src="${source}" alt="Publicação de @${escapeHtml(item.account)}" loading="lazy">`;
    let date = "";
    if (item.timestamp) {
      const parsedDate = new Date(item.timestamp);
      if (!Number.isNaN(parsedDate.getTime())) {
        date = parsedDate.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
      }
    }
    return `<article class="profile-feed-card">
      <div class="profile-feed-media">${mediaMarkup}<span class="profile-feed-type" title="${video ? "Vídeo" : "Publicação"}"><i data-lucide="${video ? "clapperboard" : "image"}"></i></span></div>
      <div class="profile-feed-details">
        <div class="profile-feed-counts">
          <span class="profile-feed-count" title="Visualizações"><i data-lucide="eye"></i>${formatCount(item.views)}</span>
          <span class="profile-feed-count" title="Curtidas"><i data-lucide="heart"></i>${formatCount(item.likes)}</span>
          <span class="profile-feed-count" title="Comentários"><i data-lucide="message-circle"></i>${formatCount(item.comments)}</span>
          <span class="profile-feed-count" title="Engajamento"><i data-lucide="chart-no-axes-column-increasing"></i>${formatCount(item.engagement)}</span>
        </div>
        <time class="profile-feed-date">${escapeHtml(date)}</time>
        ${item.caption ? `<span class="profile-feed-caption" title="${escapeHtml(item.caption)}">${escapeHtml(item.caption)}</span>` : ""}
      </div>
    </article>`;
  }).join("");
  if (window.lucide) window.lucide.createIcons();
}

async function loadSelectedProfile(accountId) {
  const button = profileButtons.find(item => item.dataset.metricsAccount === accountId);
  if (!button) return;
  selectedAccountId = accountId;
  const version = ++requestVersion;
  profileButtons.forEach(item => {
    const selected = item === button;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-pressed", String(selected));
  });
  selectedName.textContent = `@${button.dataset.username}`;
  const cachedProfile = profileCache.get(accountId);
  setMetric("followers", cachedProfile?.followers);
  setMetric("media_count", cachedProfile?.media_count);
  setMetric("following", cachedProfile?.following);
  statusMessage.textContent = "Carregando perfil e feed...";
  statusMessage.classList.remove("error");
  showMessage("Carregando mídias recentes...");
  try {
    const payload = await fetchAnalytics(`account_ids=${encodeURIComponent(accountId)}&period_days=30`);
    if (version !== requestVersion) return;
    updateProfileRows(payload.accounts || []);
    const selectedProfile = (payload.accounts || []).find(item => String(item.account_id) === accountId);
    if (selectedProfile) {
      setMetric("followers", selectedProfile.followers);
      setMetric("media_count", selectedProfile.media_count);
      setMetric("following", selectedProfile.following);
      statusMessage.textContent = "";
    }
    const accountError = (payload.errors || []).find(item => String(item.account_id) === accountId);
    if (accountError) {
      statusMessage.textContent = accountError.error || "Falha ao consultar este perfil.";
      statusMessage.classList.add("error");
      showMessage("Não foi possível consultar o Instagram para este perfil.", true);
      return;
    }
    profileCache.set(`${accountId}:media`, payload.media || []);
    renderMedia(payload.media || []);
  } catch (error) {
    if (version !== requestVersion) return;
    statusMessage.textContent = error.message;
    statusMessage.classList.add("error");
    showMessage("Falha ao carregar as métricas deste perfil.", true);
  }
}

async function loadProfiles() {
  if (!profileButtons.length) return;
  statusMessage.textContent = "Atualizando perfis...";
  statusMessage.classList.remove("error");
  try {
    const payload = await fetchAnalytics("period_days=30&include_media=false");
    updateProfileRows(payload.accounts || []);
    const listErrors = payload.errors || [];
    if (listErrors.length) {
      statusMessage.textContent = `${listErrors.length} perfil(is) não puderam ser atualizados.`;
      statusMessage.classList.add("error");
    } else {
      statusMessage.textContent = "";
    }
  } catch (error) {
    statusMessage.textContent = error.message;
    statusMessage.classList.add("error");
  }
}

engagementSort?.addEventListener("change", () => {
  if (selectedAccountId) {
    const cachedMedia = profileCache.get(`${selectedAccountId}:media`);
    if (cachedMedia) renderMedia(cachedMedia);
  }
});

profileButtons.forEach(button => {
  button.addEventListener("click", () => loadSelectedProfile(button.dataset.metricsAccount));
});

refreshButton?.addEventListener("click", async () => {
  refreshButton.disabled = true;
  try {
    await loadProfiles();
    if (selectedAccountId) await loadSelectedProfile(selectedAccountId);
  } finally {
    refreshButton.disabled = false;
  }
});

if (profileList && profileButtons.length) {
  loadProfiles().then(() => loadSelectedProfile(profileButtons[0].dataset.metricsAccount));
}
