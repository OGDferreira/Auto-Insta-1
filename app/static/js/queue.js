export function initQueueModule() {
  document.querySelectorAll("[data-calendar-status]").forEach(item => {
    item.classList.toggle("status-badge", true);
  });
  const modal = document.getElementById("batch-interval-modal");
  const form = document.getElementById("batch-interval-form");
  if (!modal || !form) return;
  const close = () => { modal.hidden = true; };
  document.getElementById("batch-interval-cancel").addEventListener("click", close);
  modal.addEventListener("click", event => { if (event.target === modal) close(); });
  document.querySelectorAll("[data-edit-batch-interval]").forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      document.getElementById("batch-interval-id").value = button.dataset.editBatchInterval;
      modal.hidden = false;
      document.getElementById("batch-interval-value").focus();
    });
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const batchId = document.getElementById("batch-interval-id").value;
    const interval = Number(document.getElementById("batch-interval-value").value);
    const response = await fetch(`/api/queue/batches/${batchId}/interval`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intervalo_minutos: interval }),
    });
    const payload = await response.json();
    if (!response.ok) {
      if (typeof window.showToast === "function") window.showToast(payload.detail || "Falha ao recalcular lote.", "error");
      return;
    }
    close();
    if (typeof window.showToast === "function") window.showToast(`${payload.updated} publicação(ões) reagendada(s).`, "success");
    window.location.reload();
  });
}

initQueueModule();
