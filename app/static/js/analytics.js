import { escapeHtml, showModuleToast } from "./dashboard.js";

export function initAnalyticsModule() {
  const section = document.getElementById("analytics");
  if (!section) return;
  const refresh = async () => {
    const response = await fetch(`/api/analytics?period_days=${document.getElementById("analytics-period").value}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) return showModuleToast(payload.detail || "Falha ao carregar analytics.", "error");
    const accounts = payload.accounts || [];
    const totals = accounts.reduce((sum, item) => ({
      followers: sum.followers + Number(item.followers || 0),
      reach: sum.reach + Number(item.reach || 0),
      impressions: sum.impressions + Number(item.impressions || 0),
      profile_views: sum.profile_views + Number(item.profile_views || 0),
    }), { followers: 0, reach: 0, impressions: 0, profile_views: 0 });
    Object.entries(totals).forEach(([key, value]) => { const node = document.querySelector(`[data-analytics-metric="${key}"]`); if (node) node.textContent = value.toLocaleString("pt-BR"); });
    document.getElementById("analytics-account-table").innerHTML = accounts.map(item => `<tr><td>@${escapeHtml(item.account)}</td><td>${item.followers.toLocaleString("pt-BR")}</td><td>${item.reach.toLocaleString("pt-BR")}</td><td>${item.impressions.toLocaleString("pt-BR")}</td><td>${item.profile_views.toLocaleString("pt-BR")}</td><td>${item.link_clicks.toLocaleString("pt-BR")}</td></tr>`).join("") || "<tr><td colspan=\"6\">Nenhum dado disponível.</td></tr>";
    const metric = value => value === null || value === undefined ? "—" : Number(value).toLocaleString("pt-BR");
    document.getElementById("analytics-media-table").innerHTML = (payload.media || []).slice(0, 50).map(item => `<tr><td>@${escapeHtml(item.account)}</td><td>${escapeHtml(item.media_type || "")}</td><td>${metric(item.likes)}</td><td>${metric(item.comments)}</td><td>${metric(item.shares)}</td><td>${metric(item.saves)}</td><td>${metric(item.views)}</td><td>${escapeHtml(item.caption || "").slice(0, 80)}</td></tr>`).join("") || "<tr><td colspan=\"8\">Nenhuma mídia disponível.</td></tr>";
  };
  document.getElementById("analytics-refresh").addEventListener("click", refresh);
  document.getElementById("analytics-period").addEventListener("change", refresh);
  refresh();
}

initAnalyticsModule();
