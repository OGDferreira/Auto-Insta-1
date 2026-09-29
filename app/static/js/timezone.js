export const BRAZIL_TIME_ZONE = "America/Sao_Paulo";

export function brazilDateParts(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: BRAZIL_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  return Object.fromEntries(parts.map(part => [part.type, part.value]));
}

export function brazilDateKey(value = new Date()) {
  const parts = brazilDateParts(value);
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function formatBrazilDateTime(value, options = {}) {
  if (!value) return "";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("pt-BR", {
    timeZone: BRAZIL_TIME_ZONE,
    ...options,
  }).format(date);
}

export function brazilDateTimeInputValue(value = new Date()) {
  const parts = brazilDateParts(value);
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

export function parseBrazilDateTimeInput(value) {
  if (!value || !value.includes("T")) return new Date(NaN);
  const [datePart, timePart] = value.split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute] = timePart.split(":").map(Number);
  const wallClock = Date.UTC(year, month - 1, day, hour, minute);
  let instant = wallClock;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const parts = brazilDateParts(new Date(instant));
    const displayedAsUtc = Date.UTC(
      Number(parts.year),
      Number(parts.month) - 1,
      Number(parts.day),
      Number(parts.hour),
      Number(parts.minute),
    );
    const correction = wallClock - displayedAsUtc;
    instant += correction;
    if (correction === 0) break;
  }
  return new Date(instant);
}
