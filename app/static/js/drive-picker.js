export async function importDriveMedia({ fileId, folderId }) {
  const body = new FormData();
  if (fileId) body.append("file_id", fileId);
  if (folderId) body.append("folder_id", folderId);
  const response = await fetch("/api/drive/import", { method: "POST", body });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Falha ao importar mídia do Drive");
  return payload.items || [];
}

export function initDrivePicker() {
  document.querySelectorAll("[data-drive-picker]").forEach(button => {
    button.addEventListener("click", () => document.getElementById(button.dataset.drivePicker)?.click());
  });
}
