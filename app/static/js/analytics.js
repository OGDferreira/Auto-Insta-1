import { escapeHtml, showModuleToast } from "./dashboard.js";

export function initAnalyticsComparison() {
  const periodSelect = document.getElementById("analytics-comparison-period");
  const startInput = document.getElementById("analytics-comparison-start");
  const endInput = document.getElementById("analytics-comparison-end");
  const accountsSelect = document.getElementById("analytics-comparison-accounts");
  const refreshButton = document.getElementById("analytics-comparison-refresh");
  const exportButton = document.getElementById("analytics-comparison-export");
  const table = document.getElementById("analytics-comparison-table");
  const status = document.getElementById("analytics-comparison-status");
  if (!periodSelect || !startInput || !endInput || !accountsSelect || !refreshButton || !exportButton || !table || !status) return;

  const metricFields = [
    ["reach", "Alcance"],
    ["impressions", "Impressões"],
    ["profile_views", "Visitas ao perfil"],
    ["link_clicks", "Cliques no link"],
  ];
  let comparisonRows = [];
  let requestVersion = 0;

  const localDateString = () => {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/Sao_Paulo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date());
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  };
  const adjustDate = (value, days) => {
    const date = new Date(`${value}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + days);
    return date.toISOString().slice(0, 10);
  };
  const numberValue = value => {
    const number = Number(value || 0);
    return Number.isFinite(number) ? number : 0;
  };
  const formatNumber = value => numberValue(value).toLocaleString("pt-BR");
  const accountIds = () => [...accountsSelect.selectedOptions].map(option => option.value);
  const buildQuery = ({ periodDays, startDate, endDate, ids }) => {
    const params = new URLSearchParams({
      period_days: String(periodDays),
      include_media: "false",
      include_account_insights: "true",
    });
    if (startDate && endDate) {
      params.set("start_date", startDate);
      params.set("end_date", endDate);
    }
    ids.forEach(id => params.append("account_ids", id));
    return params;
  };
  const fetchComparison = async params => {
    const response = await fetch(`/api/analytics?${params}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Falha ao carregar o relatório.");
    return payload;
  };

  function render(rows) {
    const headers = metricFields.map(([, label]) => `
      <th>${label}<small class="analytics-comparison-subhead">Atual · anterior · variação</small></th>
    `).join("");
    table.closest("table").querySelector("thead tr").innerHTML = `<th>Conta</th>${headers}`;
    table.innerHTML = rows.map(row => `<tr>
      <td>@${escapeHtml(row.account)}</td>
      ${metricFields.map(([key]) => `<td>
        <strong>${formatNumber(row.current[key])}</strong>
        <small class="analytics-comparison-previous">Anterior: ${formatNumber(row.previous[key])}</small>
        <small class="analytics-comparison-change">${row.change[key]}</small>
      </td>`).join("")}
    </tr>`).join("") || '<tr><td colspan="5">Nenhuma conta disponível para o período.</td></tr>';
  }

  function toCsv() {
    const cells = [["Conta", ...metricFields.flatMap(([, label]) => [
      `${label} atual`, `${label} anterior`, `${label} variação %`,
    ])]];
    comparisonRows.forEach(row => {
      const values = [row.account];
      metricFields.forEach(([key]) => {
        values.push(
          row.current[key],
          row.previous[key],
          row.previous[key] > 0
            ? (((row.current[key] - row.previous[key]) / row.previous[key]) * 100).toFixed(2)
            : row.current[key] > 0 ? "Novo" : "0",
        );
      });
      cells.push(values);
    });
    const csv = `\uFEFF${cells.map(row => row.map(value => {
      const text = String(value ?? "");
      return `"${text.replace(/"/g, '""')}"`;
    }).join(";")).join("\r\n")}`;
    const file = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(file);
    const link = document.createElement("a");
    link.href = url;
    link.download = "comparativo-analytics.csv";
    link.click();
    URL.revokeObjectURL(url);
  }

  async function compare() {
    const request = ++requestVersion;
    const period = periodSelect.value;
    const ids = accountIds();
    const isCustom = period === "custom";
    const startDate = isCustom ? startInput.value : "";
    const endDate = isCustom ? endInput.value : "";
    if (isCustom && (!startDate || !endDate || startDate > endDate)) {
      status.textContent = "Informe um intervalo válido, com data inicial anterior ou igual à final.";
      status.classList.add("error");
      return;
    }
    refreshButton.disabled = true;
    exportButton.disabled = true;
    status.textContent = "Carregando dados para comparar...";
    status.classList.remove("error");
    table.innerHTML = '<tr><td colspan="5">Carregando...</td></tr>';
    try {
      const current = await fetchComparison(buildQuery({
        periodDays: isCustom ? 30 : Number(period),
        startDate,
        endDate,
        ids,
      }));
      const duration = current.period_days;
      const previousStart = adjustDate(current.start_date, -duration);
      const previousEnd = adjustDate(current.start_date, -1);
      const previous = await fetchComparison(buildQuery({
        periodDays: duration >= 90 ? 90 : duration >= 30 ? 30 : 7,
        startDate: previousStart,
        endDate: previousEnd,
        ids,
      }));
      if (request !== requestVersion) return;
      const previousById = new Map((previous.accounts || []).map(account => [account.account_id, account]));
      comparisonRows = (current.accounts || []).map(account => {
        const old = previousById.get(account.account_id) || {};
        const currentValues = {};
        const previousValues = {};
        const change = {};
        metricFields.forEach(([key]) => {
          currentValues[key] = numberValue(account[key]);
          previousValues[key] = numberValue(old[key]);
          change[key] = previousValues[key] > 0
            ? `${(((currentValues[key] - previousValues[key]) / previousValues[key]) * 100).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`
            : currentValues[key] > 0 ? "Novo" : "0%";
        });
        return { account: account.account, current: currentValues, previous: previousValues, change };
      });
      render(comparisonRows);
      exportButton.disabled = comparisonRows.length === 0;
      const dateLabel = `${current.start_date} a ${current.end_date}`;
      const previousErrors = (previous.errors || []).length;
      const currentErrors = (current.errors || []).length;
      status.textContent = `Período atual: ${dateLabel}. Comparado aos ${duration} dias anteriores.${currentErrors || previousErrors ? ` Dados indisponíveis em ${currentErrors + previousErrors} consulta(s) de conta.` : ""}`;
      status.classList.toggle("error", Boolean(currentErrors || previousErrors));
    } catch (error) {
      if (request !== requestVersion) return;
      comparisonRows = [];
      table.innerHTML = '<tr><td colspan="5">Não foi possível carregar o comparativo.</td></tr>';
      status.textContent = error.message;
      status.classList.add("error");
      showModuleToast(error.message, "error");
    } finally {
      if (request === requestVersion) refreshButton.disabled = false;
    }
  }

  periodSelect.addEventListener("change", () => {
    const custom = periodSelect.value === "custom";
    document.querySelectorAll(".analytics-custom-date").forEach(label => {
      label.hidden = !custom;
    });
    if (custom && !startInput.value) startInput.value = adjustDate(localDateString(), -29);
    if (custom && !endInput.value) endInput.value = localDateString();
    if (!custom) compare();
  });
  refreshButton.addEventListener("click", compare);
  exportButton.addEventListener("click", toCsv);
  [startInput, endInput, accountsSelect].forEach(element => {
    element.addEventListener("change", () => {
      if (periodSelect.value !== "custom" || element === accountsSelect) compare();
    });
  });
  document.querySelectorAll(".analytics-custom-date").forEach(label => {
    label.hidden = periodSelect.value !== "custom";
  });
  compare();
}

initAnalyticsComparison();
