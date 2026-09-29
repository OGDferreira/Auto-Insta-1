import { formatBrazilDateTime } from "./timezone.js";

const profileButtons = [...document.querySelectorAll("[data-metrics-account]")];
const profileList = document.getElementById("metrics-profile-list");
const profileFeed = document.getElementById("profile-feed");
const statusMessage = document.getElementById("profile-metrics-status");
const feedCount = document.getElementById("profile-feed-count");
const selectedName = document.getElementById("selected-profile-name");
const refreshButton = document.getElementById("profile-metrics-refresh");
const engagementSort = document.getElementById("profile-engagement-sort");
const accountEngagementSort = document.getElementById("profile-account-engagement-sort");
const accountSortStatus = document.getElementById("profile-account-sort-status");
const profileCache = new Map();
const originalProfileOrder = new Map(profileButtons.map((button, index) => [button, index]));
let selectedAccountId = null;
let requestVersion = 0;
let accountSortRequestVersion = 0;
let accountEngagementScores = null;

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

function metricCount(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function sortProfileButtons(scores) {
  const direction = accountEngagementSort?.value || "default";
  const ordered = [...profileButtons].sort((left, right) => {
    if (direction === "default" || !scores) {
      return originalProfileOrder.get(left) - originalProfileOrder.get(right);
    }
    const leftScore = scores.get(left.dataset.metricsAccount);
    const rightScore = scores.get(right.dataset.metricsAccount);
    if (leftScore === undefined || rightScore === undefined) {
      if (leftScore === rightScore) return originalProfileOrder.get(left) - originalProfileOrder.get(right);
      return leftScore === undefined ? 1 : -1;
    }
    const difference = direction === "asc" ? leftScore - rightScore : rightScore - leftScore;
    return difference || originalProfileOrder.get(left) - originalProfileOrder.get(right);
  });
  profileList?.append(...ordered);
}

function collectAccountEngagement(payload) {
  const totals = new Map();
  (payload.media || []).forEach(item => {
    const accountId = String(item.account_id ?? "");
    if (!accountId) return;
    const interactions = ["likes", "comments", "shares", "saves"]
      .map(key => metricCount(item[key]))
      .filter(value => value !== null);
    const reportedTotal = metricCount(item.engagement);
    const views = metricCount(item.views);
    const impressions = metricCount(item.impressions);
    const current = totals.get(accountId) || { interactions: 0, hasInteractions: false, views: 0, impressions: 0 };
    if (reportedTotal !== null) {
      current.interactions += reportedTotal;
      current.hasInteractions = true;
    } else if (interactions.length) {
      current.interactions += interactions.reduce((total, value) => total + value, 0);
      current.hasInteractions = true;
    }
    if (views !== null) current.views += views;
    if (impressions !== null) current.impressions += impressions;
    totals.set(accountId, current);
  });
  const accountImpressions = new Map((payload.accounts || []).map(account => [
    String(account.account_id),
    metricCount(account.impressions),
  ]));
  return new Map([...totals].flatMap(([accountId, value]) => {
    const impressions = accountImpressions.get(accountId);
    const exposure = impressions > 0 ? impressions : value.impressions || value.views;
    if (!value.hasInteractions || exposure <= 0) return [];
    return [[accountId, value.interactions / exposure * 100]];
  }));
}

function renderAccountEngagement(scores) {
  profileButtons.forEach(button => {
    const node = button.querySelector("[data-account-engagement]");
    if (!node) return;
    const score = scores.get(button.dataset.metricsAccount);
    node.hidden = false;
    node.textContent = score === undefined
      ? "Engajamento indisponível"
      : `${score.toLocaleString("pt-BR", { maximumFractionDigits: 2 })}% de engajamento`;
  });
}

async function updateAccountOrder() {
  const request = ++accountSortRequestVersion;
  if (!accountEngagementSort || accountEngagementSort.value === "default") {
    sortProfileButtons(null);
    if (accountSortStatus) {
      accountSortStatus.textContent = "Interações das publicações divididas pelas impressões (ou visualizações disponíveis).";
      accountSortStatus.classList.remove("error");
    }
    return;
  }
  if (accountEngagementScores === null) {
    if (accountSortStatus) {
      accountSortStatus.textContent = "Calculando engajamento das publicações recentes...";
      accountSortStatus.classList.remove("error");
    }
    try {
      const payload = await fetchAnalytics("period_days=30");
      if (request !== accountSortRequestVersion) return;
      updateProfileRows(payload.accounts || []);
      accountEngagementScores = collectAccountEngagement(payload);
      renderAccountEngagement(accountEngagementScores);
      const unavailableCount = profileButtons.filter(button => (
        !accountEngagementScores.has(button.dataset.metricsAccount)
      )).length;
      const errorCount = (payload.errors || []).length;
      if (accountSortStatus) {
        accountSortStatus.textContent = errorCount || unavailableCount
          ? `Dados incompletos: ${unavailableCount} conta(s) sem métricas e ${errorCount} erro(s) na consulta.`
          : "Interações das publicações divididas pelas impressões (ou visualizações disponíveis).";
        accountSortStatus.classList.toggle("error", Boolean(errorCount || unavailableCount));
      }
    } catch (error) {
      if (request !== accountSortRequestVersion) return;
      sortProfileButtons(null);
      if (accountSortStatus) {
        accountSortStatus.textContent = `Não foi possível ordenar: ${error.message}`;
        accountSortStatus.classList.add("error");
      }
      return;
    }
  }
  if (request === accountSortRequestVersion) sortProfileButtons(accountEngagementScores);
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
        date = formatBrazilDateTime(parsedDate, {
          day: "2-digit",
          month: "2-digit",
          year: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          hourCycle: "h23",
        });
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

accountEngagementSort?.addEventListener("change", updateAccountOrder);

profileButtons.forEach(button => {
  button.addEventListener("click", () => loadSelectedProfile(button.dataset.metricsAccount));
});

refreshButton?.addEventListener("click", async () => {
  refreshButton.disabled = true;
  try {
    accountEngagementScores = null;
    await loadProfiles();
    if (accountEngagementSort?.value !== "default") await updateAccountOrder();
    if (selectedAccountId) await loadSelectedProfile(selectedAccountId);
  } finally {
    refreshButton.disabled = false;
  }
});

if (profileList && profileButtons.length) {
  loadProfiles().then(() => loadSelectedProfile(profileButtons[0].dataset.metricsAccount));
}
