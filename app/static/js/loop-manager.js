function setEditorVisibility(editor, visible) {
  if (editor) editor.hidden = !visible;
}

export function initLoopManager() {
  const loopTabs = [...document.querySelectorAll("[data-loop-view]")];
  const loopPanels = [...document.querySelectorAll("[data-loop-panel]")];
  loopTabs.forEach(tab => {
    tab.addEventListener("click", () => {
      const selectedView = tab.dataset.loopView;
      loopTabs.forEach(item => {
        const selected = item === tab;
        item.classList.toggle("active", selected);
        item.setAttribute("aria-selected", String(selected));
      });
      loopPanels.forEach(panel => {
        panel.hidden = panel.dataset.loopPanel !== selectedView;
      });
    });
  });

  document.querySelectorAll("[data-loop-details-toggle]").forEach(button => {
    button.addEventListener("click", () => {
      const details = document.querySelector(`#loop-details-${button.dataset.loopDetailsToggle}`);
      if (!details) return;
      const expanded = button.getAttribute("aria-expanded") !== "true";
      details.hidden = !expanded;
      button.setAttribute("aria-expanded", String(expanded));
      const label = expanded ? "Ocultar publicações e horários" : "Ver publicações e horários";
      button.setAttribute("aria-label", label);
      button.title = label;
      button.innerHTML = `<i data-lucide="${expanded ? "eye-off" : "eye"}"></i>`;
      if (window.lucide) window.lucide.createIcons();
    });
  });

  document.querySelectorAll("[data-loop-edit]").forEach(button => {
    button.addEventListener("click", () => {
      const editor = document.querySelector(`[data-loop-editor="${button.dataset.loopEdit}"]`);
      setEditorVisibility(editor, true);
      editor?.querySelector('input[name="batch_name"]')?.focus();
    });
  });

  document.querySelectorAll("[data-loop-cancel]").forEach(button => {
    button.addEventListener("click", () => {
      setEditorVisibility(document.querySelector(`[data-loop-editor="${button.dataset.loopCancel}"]`), false);
    });
  });

  document.querySelectorAll("[data-loop-create]").forEach(button => {
    button.addEventListener("click", () => {
      const composer = document.querySelector("#bulk-form");
      const loopToggle = document.getElementById("loop-enabled");
      if (loopToggle && !loopToggle.checked) {
        loopToggle.checked = true;
        loopToggle.dispatchEvent(new Event("change", { bubbles: true }));
      }
      composer?.scrollIntoView({ behavior: "smooth", block: "center" });
      composer?.querySelector('input[name="batch_name"]')?.focus({ preventScroll: true });
    });
  });
}

initLoopManager();
