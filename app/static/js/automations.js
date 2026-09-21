export function initAutomationsModule() {
  const section = document.getElementById("automations");
  if (!section) return;
  section.dataset.moduleReady = "true";
}
