const form = document.getElementById("direct-campaign-form");
if (form) {
  const accountSelect = document.getElementById("direct-campaign-accounts");
  const contactsList = document.getElementById("direct-campaign-contacts");
  const summary = document.getElementById("direct-campaign-summary");
  async function loadContacts() {
    const ids = [...accountSelect.selectedOptions].map(option => option.value);
    if (!ids.length) {
      contactsList.innerHTML = "<p class='muted'>Selecione uma ou mais contas.</p>";
      summary.textContent = "";
      return;
    }
    const response = await fetch(`/api/direct/campaign/contacts?${ids.map(id => `account_ids=${encodeURIComponent(id)}`).join("&")}`, {cache: "no-store"});
    const result = await response.json();
    if (!response.ok) return window.showToast?.(result.detail || "Não foi possível carregar contatos.", "error");
    contactsList.innerHTML = (result.contacts || []).map(contact =>
      `<label class="campaign-contact"><input type="checkbox" name="contact_ids" value="${contact.id}" checked> Conta ${contact.account_id} · contato elegível</label>`
    ).join("") || "<p class='muted'>Nenhum contato dentro da janela de 24 horas.</p>";
    summary.textContent = `${result.eligible_count} contato(s) elegível(is). Janela: ${result.window_hours}h.`;
  }
  accountSelect.addEventListener("change", loadContacts);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const accountIds = [...accountSelect.selectedOptions].map(option => Number(option.value));
    const contactIds = [...form.querySelectorAll("[name=contact_ids]:checked")].map(input => Number(input.value));
    const message = document.getElementById("direct-campaign-message").value.trim();
    if (!accountIds.length || !contactIds.length || !message) return window.showToast?.("Selecione contas, contatos e informe a mensagem.", "error");
    if (!confirm(`Enviar a mensagem para ${contactIds.length} contato(s) elegível(is), com intervalo de 2 segundos?`)) return;
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const response = await fetch("/api/direct/campaigns", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({account_ids: accountIds, contact_ids: contactIds, message}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Falha ao processar campanha.");
      window.showToast?.(`${result.sent.length} mensagem(ns) enviada(s). ${result.errors.length} falha(s).`, result.errors.length ? "error" : "success");
      loadContacts();
    } catch (error) {
      window.showToast?.(error.message, "error");
    } finally {
      submit.disabled = false;
    }
  });
}
