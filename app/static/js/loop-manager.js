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
      const composerPanel = document.querySelector("#loop-composer");
      if (composerPanel) composerPanel.hidden = false;
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

// Playlist editor: upload new media while preserving the loop schedule and account assignments.
document.querySelectorAll("[data-loop-media-edit]").forEach(button => {
  button.addEventListener("click", () => {
    const editor = document.querySelector(`[data-loop-media-editor="${button.dataset.loopMediaEdit}"]`);
    if (!editor) return;
    editor.hidden = !editor.hidden;
    if (!editor.hidden) editor.querySelector("[data-loop-media-files]")?.focus();
  });
});
document.querySelectorAll("[data-loop-media-editor]").forEach(editor => {
  const input = editor.querySelector("[data-loop-media-files]");
  const items = editor.querySelector("[data-loop-media-items]");
  let uploads = [];
  input?.addEventListener("change", async () => {
    uploads = [];
    for (const file of [...input.files]) {
      const body = new FormData();
      body.append("media", file, file.name);
      const response = await fetch("/media/upload", { method: "POST", body });
      if (!response.ok) { window.showToast?.(`Falha no upload de ${file.name}.`, "error"); continue; }
      const payload = await response.json();
      uploads.push({ ...payload, name: file.name });
    }
    if (items) items.innerHTML = uploads.map((item, index) =>
      `<div class="thumb-item"><img class="thumb" src="${item.url}" alt=""><span><strong>${index + 1}. ${item.name}</strong><br><small>${item.media_type}</small></span></div>`
    ).join("");
  });
  editor.addEventListener("submit", event => {
    if (!uploads.length) {
      event.preventDefault();
      window.showToast?.("Selecione as novas mídias do Loop.", "error");
      return;
    }
    editor.querySelectorAll(".generated-loop-media").forEach(item => item.remove());
    uploads.forEach(item => {
      [["media_urls", item.url], ["media_types", item.media_type], ["storage_paths", item.storage_path || ""]].forEach(([name, value]) => {
        const hidden = document.createElement("input");
        hidden.type = "hidden"; hidden.name = name; hidden.value = value;
        hidden.className = "generated-loop-media"; editor.appendChild(hidden);
      });
    });
  });
});
