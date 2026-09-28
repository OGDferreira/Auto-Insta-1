const bulkForm = document.getElementById("bulk-form");
if (bulkForm) {
  const draftKey = "auto-insta-bulk-draft";
  const draftFields = [...bulkForm.querySelectorAll("input:not([type=file]), select, textarea")];
  const draft = JSON.parse(localStorage.getItem(draftKey) || "{}");
  draftFields.forEach(field => {
    if (draft[field.name] !== undefined && field.type !== "radio" && field.type !== "checkbox") field.value = draft[field.name];
    if (field.type === "radio" && draft[field.name] === field.value) field.checked = true;
    field.addEventListener("input", () => {
      const current = {};
      draftFields.forEach(item => {
        if (item.type === "radio" && !item.checked) return;
        current[item.name] = item.value;
      });
      localStorage.setItem(draftKey, JSON.stringify(current));
    });
  });
  bulkForm.addEventListener("submit", () => localStorage.removeItem(draftKey));
}

const selectionButton = document.getElementById("select-all-posts");
const clearSelectionButton = document.createElement("button");
if (selectionButton && selectionButton.parentElement) {
  clearSelectionButton.type = "button";
  clearSelectionButton.className = "secondary privacy-button";
  clearSelectionButton.title = "Limpar seleção";
  clearSelectionButton.setAttribute("aria-label", "Limpar seleção");
  clearSelectionButton.innerHTML = '<i data-lucide="list-x"></i>';
  selectionButton.parentElement.insertBefore(clearSelectionButton, selectionButton.nextSibling);
  clearSelectionButton.addEventListener("click", () => {
    document.querySelectorAll(".queue-select").forEach(input => {
      input.checked = false;
      input.closest(".queue-card")?.classList.remove("selected");
    });
    selectionButton.checked = false;
    selectionButton.indeterminate = false;
    document.querySelector("#delete-selected-form button")?.setAttribute("hidden", "");
  });
  if (window.lucide) window.lucide.createIcons();
}

document.querySelectorAll("[data-metric='pending'], [data-metric='failed'], [data-metric='error_accounts']").forEach(metric => {
  const card = metric.closest(".metric");
  if (card) {
    card.tabIndex = 0;
    card.addEventListener("click", () => {
      const target = metric.dataset.metric === "error_accounts" ? "accounts" : "queue";
      document.querySelector(`[data-view="${target}"]`)?.click();
    });
    card.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") card.click();
    });
  }
});
