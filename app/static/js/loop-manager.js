function setEditorVisibility(editor, visible) {
  if (editor) editor.hidden = !visible;
}

export function initLoopManager() {
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
