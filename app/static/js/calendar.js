import { escapeHtml, showModuleToast } from "./dashboard.js";
import { brazilDateKey, brazilDateParts, formatBrazilDateTime } from "./timezone.js";

const todayParts = brazilDateParts();
const state = {
  date: new Date(Date.UTC(Number(todayParts.year), Number(todayParts.month) - 1, Number(todayParts.day))),
  view: "month",
  items: [],
};
const pad = value => String(value).padStart(2, "0");
const dateKey = date => `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;

function render() {
  const grid = document.getElementById("calendar-grid");
  if (!grid) return;
  const first = new Date(Date.UTC(state.date.getUTCFullYear(), state.date.getUTCMonth(), 1));
  const start = new Date(first);
  start.setUTCDate(1 - first.getUTCDay());
  const days = Array.from({ length: 42 }, (_, index) => {
    const day = new Date(start);
    day.setUTCDate(start.getUTCDate() + index);
    const key = dateKey(day);
    const items = state.items.filter(item => brazilDateKey(item.scheduled_for) === key);
    return `<div class="calendar-day ${day.getUTCMonth() !== state.date.getUTCMonth() ? "outside" : ""}" data-calendar-date="${key}">
      <div class="calendar-day-number">${day.getUTCDate()}</div>
      ${items.map(item => `<div class="calendar-event ${item.status === "failed" ? "failed" : ""} ${item.conflict ? "conflict" : ""}" draggable="true" data-calendar-id="${item.id}" title="${escapeHtml(item.error_message || item.caption || "")}">
        <img src="${escapeHtml(item.media_url || "")}" alt=""><span>@${escapeHtml(item.account)} · ${formatBrazilDateTime(item.scheduled_for, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })} · ${escapeHtml(item.status)}</span>
      </div>`).join("")}
    </div>`;
  });
  grid.innerHTML = `<div class="calendar-weekday">Dom</div><div class="calendar-weekday">Seg</div><div class="calendar-weekday">Ter</div><div class="calendar-weekday">Qua</div><div class="calendar-weekday">Qui</div><div class="calendar-weekday">Sex</div><div class="calendar-weekday">Sáb</div>${days.join("")}`;
  bindDrag();
  document.getElementById("calendar-title").textContent = new Intl.DateTimeFormat("pt-BR", {
    timeZone: "UTC",
    month: "long",
    year: "numeric",
  }).format(state.date);
}

function bindDrag() {
  let dragged = null;
  document.querySelectorAll("[data-calendar-id]").forEach(event => event.addEventListener("dragstart", () => { dragged = event.dataset.calendarId; }));
  document.querySelectorAll("[data-calendar-date]").forEach(day => {
    day.addEventListener("dragover", event => { event.preventDefault(); day.classList.add("drag-over"); });
    day.addEventListener("dragleave", () => day.classList.remove("drag-over"));
    day.addEventListener("drop", async event => {
      event.preventDefault(); day.classList.remove("drag-over");
      const item = state.items.find(entry => String(entry.id) === String(dragged));
      if (!item) return;
      const original = brazilDateParts(new Date(item.scheduled_for));
      const target = `${day.dataset.calendarDate}T${original.hour}:${original.minute}`;
      const response = await fetch(`/api/calendar/posts/${item.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scheduled_for: target }) });
      if (!response.ok) return showModuleToast((await response.json()).detail || "Falha ao reagendar.", "error");
      await load();
    });
  });
}

async function load() {
  const params = new URLSearchParams();
  const account = document.getElementById("calendar-account-filter")?.value;
  const status = document.getElementById("calendar-status-filter")?.value;
  if (account) params.set("account_id", account);
  if (status) params.set("status", status);
  const response = await fetch(`/api/calendar/posts?${params}`, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) return showModuleToast(payload.detail || "Falha ao carregar calendário.", "error");
  state.items = payload.items || [];
  render();
}

export function initCalendarModule() {
  if (!document.getElementById("calendar-grid")) return;
  document.getElementById("calendar-prev").addEventListener("click", () => { state.date.setUTCMonth(state.date.getUTCMonth() - 1); render(); });
  document.getElementById("calendar-next").addEventListener("click", () => { state.date.setUTCMonth(state.date.getUTCMonth() + 1); render(); });
  document.getElementById("calendar-today").addEventListener("click", () => {
    const parts = brazilDateParts();
    state.date = new Date(Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)));
    render();
  });
  document.getElementById("calendar-refresh").addEventListener("click", load);
  ["calendar-account-filter", "calendar-status-filter"].forEach(id => {
    document.getElementById(id).addEventListener("change", load);
  });
  load();
}

initCalendarModule();
