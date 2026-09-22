export function initQueueModule() {
  document.querySelectorAll("[data-calendar-status]").forEach(item => item.classList.add("status-badge"));
  document.querySelectorAll("[data-show-batch]").forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      const batchQueue = button.closest(".batch-queue") || button.closest(".batch-content")?.querySelector("[data-batch-queue]");
      if (!batchQueue) return;
      const overflow = button.parentElement?.querySelector(".batch-overflow");
      if (overflow) {
        overflow.hidden = false;
        button.remove();
        return;
      }
      batchQueue.hidden = false;
      button.remove();
    });
  });
  document.querySelectorAll("[data-account-filter]").forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      const queue = button.closest(".batch-queue");
      if (!queue) return;
      queue.hidden = false;
      const filter = button.dataset.accountFilter;
      queue.querySelectorAll("[data-account-filter]").forEach(item => item.classList.toggle("active", item === button));
      queue.querySelectorAll("[data-account-queue]").forEach(group => {
        group.hidden = filter !== "all" && group.dataset.accountQueue !== filter;
      });
    });
  });
  document.querySelectorAll("[data-account-summary]").forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      const content = button.closest(".batch-content");
      const queue = content?.querySelector("[data-batch-queue]");
      const filterButton = queue?.querySelector(`[data-account-filter="${button.dataset.accountSummary}"]`);
      if (queue && filterButton) {
        queue.hidden = false;
        filterButton.click();
        content.querySelector(":scope > [data-show-batch]")?.remove();
      }
    });
    const applyBatchStatusFilter = (batch, status) => {
      const queue = batch?.querySelector("[data-batch-queue]");
      if (!queue) return;
      queue.hidden = false;
      queue.dataset.statusFilter = status;
      queue.querySelectorAll("[data-account-queue]").forEach(group => {
        const visible = [...group.querySelectorAll(".queue-card")].some(card => {
          const matches = status === "failed"
            ? ["failed", "blocked"].includes(card.dataset.status)
            : status === "pending"
              ? ["scheduled", "pending", "aguardando"].includes(card.dataset.status)
              : card.dataset.status === status;
          card.hidden = !matches;
          return matches;
        });
        group.hidden = !visible;
      });
      batch.open = true;
    };
    document.querySelectorAll("[data-batch-status-filter]").forEach(button => {
      button.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        applyBatchStatusFilter(button.closest(".batch-accordion"), button.dataset.batchStatusFilter);
      });
    });
  });
  const modal = document.getElementById("batch-interval-modal");
  const form = document.getElementById("batch-interval-form");
  if (!modal || !form) return;

  const close = () => { modal.hidden = true; };
  document.getElementById("batch-interval-cancel")?.addEventListener("click", close);
  document.getElementById("batch-interval-close")?.addEventListener("click", close);
  modal.addEventListener("click", event => { if (event.target === modal) close(); });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !modal.hidden) close();
  });

  document.querySelectorAll("[data-edit-batch-config], [data-edit-batch-interval]").forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      document.getElementById("batch-interval-id").value = button.dataset.editBatchInterval;
      document.getElementById("batch-interval-id").value = button.dataset.editBatchConfig || button.dataset.editBatchInterval;
      document.getElementById("batch-name-value").value = button.dataset.batchName || "";
      document.getElementById("batch-start-value").value = "";
      modal.hidden = false;
      document.getElementById("batch-interval-value").focus();
    });
  });

  document.querySelectorAll("[data-toggle-batch-previews]").forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      const batch = button.closest(".batch-accordion");
      const hidden = batch.classList.toggle("batch-previews-hidden");
      button.title = hidden ? "Mostrar pré-visualizações" : "Ocultar pré-visualizações";
      button.setAttribute("aria-label", button.title);
      button.innerHTML = `<i data-lucide="${hidden ? "eye" : "eye-off"}"></i>`;
      if (window.lucide) window.lucide.createIcons();
    });
  });
  document.querySelectorAll("[data-batch-actions] form, [data-batch-actions] button").forEach(action => {
    action.addEventListener("click", event => event.stopPropagation());
  });

  document.querySelectorAll("[data-retry-batch]").forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      const retryModal = document.createElement("div");
      retryModal.className = "retry-modal";
      retryModal.innerHTML = `<div class="retry-dialog" role="dialog" aria-modal="true" aria-labelledby="batch-retry-title">
        <div class="panel-header"><h3 id="batch-retry-title">Reenviar falhas do lote</h3><button type="button" class="secondary drive-close" data-retry-close aria-label="Fechar"><i data-lucide="x"></i></button></div>
        <p class="muted">Qual o intervalo (em minutos) entre as publicações?</p>
        <label>Intervalo<input type="number" min="1" max="1440" value="1" required data-retry-interval></label>
        <div class="queue-toolbar-actions"><button type="button" class="secondary" data-retry-close>Cancelar</button><button type="button" data-retry-confirm>Confirmar</button></div>
      </div>`;
      document.body.appendChild(retryModal);
      if (window.lucide) window.lucide.createIcons();
      const closeRetry = () => retryModal.remove();
      retryModal.querySelectorAll("[data-retry-close]").forEach(closeButton => closeButton.addEventListener("click", closeRetry));
      retryModal.addEventListener("click", event => { if (event.target === retryModal) closeRetry(); });
      retryModal.querySelector("[data-retry-confirm]").addEventListener("click", async confirmEvent => {
        const confirm = confirmEvent.currentTarget;
        const interval = Number(retryModal.querySelector("[data-retry-interval]").value);
        if (!Number.isInteger(interval) || interval < 1 || interval > 1440) return;
        confirm.disabled = true;
        try {
          const response = await fetch(`/api/queue/batches/${button.dataset.retryBatch}/retry`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ intervalo_minutos: interval }),
          });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || "Falha ao reenviar o lote.");
          closeRetry();
          window.showToast?.(`${payload.updated} falha(s) reagendada(s).`, "success");
          window.location.reload();
        } catch (error) {
          window.showToast?.(error.message, "error");
          confirm.disabled = false;
        }
      });
    });
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const batchId = document.getElementById("batch-interval-id").value;
    const interval = Number(document.getElementById("batch-interval-value").value);
    const name = document.getElementById("batch-name-value").value.trim();
    const scheduledFor = document.getElementById("batch-start-value").value;
    if (!name || !Number.isInteger(interval) || interval < 1 || interval > 1440) {
      window.showToast?.("Informe um nome e um intervalo válido.", "error");
      return;
    }
    try {
      const response = await fetch(`/api/queue/batches/${batchId}/config`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, intervalo_minutos: interval, scheduled_for: scheduledFor }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Falha ao recalcular lote.");
      close();
      window.showToast?.(`${payload.updated} publicação(ões) reagendada(s).`, "success");
      window.location.reload();
    } catch (error) {
      window.showToast?.(error.message, "error");
    }
  });
}

initQueueModule();
