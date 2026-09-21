import { escapeHtml, showModuleToast } from "./dashboard.js";

let rules = [];
let media = { media_url: null, drive_media_url: null, drive_account_email: null, drive_credentials_encrypted: null };

function accountIds() {
  return [...document.getElementById("automation-account").selectedOptions].map(option => Number(option.value));
}

function resetForm() {
  const form = document.getElementById("automation-form");
  form.reset();
  document.getElementById("automation-edit-id").value = "";
  document.getElementById("automation-media-status").hidden = true;
  document.getElementById("automation-cancel").hidden = true;
  document.getElementById("automation-submit").textContent = "Salvar automação";
  media = { media_url: null, drive_media_url: null, drive_account_email: null, drive_credentials_encrypted: null };
}

function fillForm(rule) {
  document.getElementById("automation-edit-id").value = rule.id;
  document.getElementById("automation-type").value = rule.rule_type;
  document.getElementById("automation-keywords").value = rule.trigger_keywords || "";
  document.getElementById("automation-message").value = rule.message_text || "";
  const targets = new Set(rule.target_account_ids || (rule.account_id ? [rule.account_id] : []));
  [...document.getElementById("automation-account").options].forEach(option => { option.selected = Number(option.value) === 0 ? !targets.size : targets.has(Number(option.value)); });
  media = { media_url: rule.media_url, drive_media_url: rule.drive_media_url, drive_account_email: rule.drive_account_email, drive_credentials_encrypted: rule.drive_credentials_encrypted };
  const status = document.getElementById("automation-media-status");
  status.hidden = !(media.media_url || media.drive_media_url);
  status.textContent = status.hidden ? "" : "Anexo atual mantido";
  document.getElementById("automation-cancel").hidden = false;
  document.getElementById("automation-submit").textContent = "Salvar alterações";
  document.getElementById("automations").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderRules() {
  const list = document.getElementById("automation-list");
  list.innerHTML = rules.map(rule => `<article class="automation-card">
    <header><strong>${rule.rule_type === "comment_reply" ? "Comentário" : "Direct (DM)"}</strong><div class="automation-card-actions">
      <button type="button" class="text-button" data-edit-automation="${rule.id}" title="Editar"><i data-lucide="pencil"></i></button>
      <button type="button" class="text-button" data-delete-automation="${rule.id}" title="Excluir"><i data-lucide="trash-2"></i></button>
    </div></header>
    <small class="muted">${rule.target_account_ids?.length ? `${rule.target_account_ids.length} conta(s)` : "Todas as contas"} · Palavra-chave: <b>${escapeHtml(rule.trigger_keywords || "—")}</b></small>
    <p>${escapeHtml(rule.message_text || "Resposta com mídia")}</p>
    ${rule.media_url || rule.drive_media_url ? '<div class="automation-media-preview">Anexo de mídia</div>' : ""}
    <label class="select-all-accounts"><input type="checkbox" data-toggle-automation="${rule.id}" ${rule.is_active ? "checked" : ""}> Ativa</label>
  </article>`).join("") || "<p class=\"muted\">Nenhuma automação cadastrada.</p>";
  if (window.lucide) window.lucide.createIcons();
}

async function loadRules() {
  const response = await fetch("/api/automations", { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) return showModuleToast(payload.detail || "Falha ao carregar automações.", "error");
  rules = payload.items || [];
  renderRules();
}

async function save(event) {
  event.preventDefault();
  event.stopImmediatePropagation();
  const keywords = document.getElementById("automation-keywords").value.trim();
  if (!keywords) return showModuleToast("Informe a palavra-chave do gatilho.", "error");
  const id = document.getElementById("automation-edit-id").value;
  const payload = {
    target_account_ids: accountIds(),
    rule_type: document.getElementById("automation-type").value,
    trigger_keywords: keywords,
    message_text: document.getElementById("automation-message").value.trim(),
    ...media,
    is_active: true,
  };
  const response = await fetch(id ? `/api/automations/${id}` : "/api/automations", {
    method: id ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) return showModuleToast(result.detail || "Não foi possível salvar a automação.", "error");
  showModuleToast(id ? "Automação atualizada." : "Automação salva.", "success");
  resetForm();
  loadRules();
}

export function initAutomationsModule() {
  const form = document.getElementById("automation-form");
  if (!form) return;
  form.addEventListener("submit", save, true);
  document.addEventListener("automation-media-selected", event => { media = { ...event.detail }; });
  document.getElementById("automation-account")?.addEventListener("change", event => {
    const select = event.currentTarget;
    const selected = [...select.selectedOptions];
    const global = selected.some(option => option.value === "0");
    if (global && selected.length > 1) select.options[0].selected = false;
    else if (global) [...select.options].forEach(option => { option.selected = option.value === "0"; });
    else if (![...select.selectedOptions].length) select.options[0].selected = true;
  });
  document.getElementById("automation-cancel")?.addEventListener("click", resetForm);
  document.getElementById("automation-refresh")?.addEventListener("click", loadRules);
  document.getElementById("automation-file")?.addEventListener("change", async event => {
    event.stopImmediatePropagation();
    const file = event.target.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.append("media", file, file.name);
    const response = await fetch("/media/upload", { method: "POST", body });
    const payload = await response.json();
    if (!response.ok) return showModuleToast(payload.detail || "Falha no upload.", "error");
    media = { media_url: payload.url, drive_media_url: null, drive_account_email: null, drive_credentials_encrypted: null };
    const status = document.getElementById("automation-media-status");
    status.hidden = false;
    status.textContent = `Anexo selecionado: ${file.name}`;
  }, true);
  document.getElementById("automation-list")?.addEventListener("click", async event => {
    const edit = event.target.closest("[data-edit-automation]");
    if (edit) return fillForm(rules.find(rule => rule.id === Number(edit.dataset.editAutomation)));
    const remove = event.target.closest("[data-delete-automation]");
    if (remove) {
      if (!confirm("Excluir esta automação?")) return;
      await fetch(`/api/automations/${remove.dataset.deleteAutomation}`, { method: "DELETE" });
      return loadRules();
    }
  });
  document.getElementById("automation-list")?.addEventListener("change", async event => {
    const input = event.target.closest("[data-toggle-automation]");
    if (!input) return;
    await fetch(`/api/automations/${input.dataset.toggleAutomation}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ is_active: input.checked }) });
  });
  loadRules();
}

initAutomationsModule();
