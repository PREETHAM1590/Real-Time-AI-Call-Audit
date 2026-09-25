"use strict";

const byId = (id) => document.getElementById(id);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

async function getJson(path) {
  const response = await fetch(path, { credentials: "include", headers: { Accept: "application/json" } });
  let payload = {};
  try { payload = await response.json(); } catch { /* use status below */ }
  if (!response.ok) {
    const message = typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status})`;
    throw Object.assign(new Error(message), { status: response.status });
  }
  return payload;
}

function humanizeAge(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "unavailable";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function kpiTile(label, value, note) {
  const tile = element("div", undefined, "kpi-tile");
  tile.append(element("span", label, "kpi-label"), element("strong", value, "kpi-value"));
  if (note) tile.append(element("span", note, "kpi-note"));
  return tile;
}

function failedTile(label) {
  const tile = element("div", undefined, "kpi-tile kpi-failed");
  tile.append(element("span", label, "kpi-label"), element("strong", "Unavailable", "kpi-value"), element("span", "This value failed to load; it is not zero.", "kpi-note"));
  return tile;
}

function forbiddenTile(label) {
  const tile = element("div", undefined, "kpi-tile kpi-forbidden");
  tile.append(element("span", label, "kpi-label"), element("strong", "Not available for your role", "kpi-value"));
  return tile;
}

function barChart(items, { labelKey, valueKey, max }) {
  const width = 260;
  const barHeight = 18;
  const gap = 6;
  const height = items.length * (barHeight + gap) || barHeight;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", items.map((item) => `${item[labelKey]}: ${item[valueKey]}`).join(", ") || "No data");
  svg.classList.add("bar-chart");
  const scaleMax = max || Math.max(1, ...items.map((item) => item[valueKey]));
  items.forEach((item, index) => {
    const y = index * (barHeight + gap);
    const barWidth = Math.max(2, (item[valueKey] / scaleMax) * (width - 90));
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", "84");
    rect.setAttribute("y", String(y));
    rect.setAttribute("width", String(barWidth));
    rect.setAttribute("height", String(barHeight - 2));
    rect.setAttribute("class", `bar bar-${String(item[labelKey]).toLowerCase().replace(/[^a-z0-9]+/g, "-")}`);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", "0");
    label.setAttribute("y", String(y + barHeight - 4));
    label.setAttribute("class", "bar-label");
    label.textContent = String(item[labelKey]);
    const valueLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
    valueLabel.setAttribute("x", String(90 + barWidth));
    valueLabel.setAttribute("y", String(y + barHeight - 4));
    valueLabel.setAttribute("class", "bar-value");
    valueLabel.textContent = String(item[valueKey]);
    svg.append(rect, label, valueLabel);
  });
  return svg;
}

function pendingByStageTable(pendingByStage) {
  const table = element("table", undefined, "pending-by-stage-table");
  const headRow = element("tr");
  headRow.append(element("th", "Stage"), element("th", "Pending"), element("th", "Oldest pending age"));
  const thead = element("thead");
  thead.append(headRow);
  const tbody = element("tbody");
  for (const stage of Object.keys(pendingByStage).sort()) {
    const row = element("tr");
    row.append(
      element("td", stage),
      element("td", pendingByStage[stage].pending),
      element("td", humanizeAge(pendingByStage[stage].oldest_pending_age_seconds)),
    );
    tbody.append(row);
  }
  table.append(thead, tbody);
  return table;
}

function renderPendingByStage(pendingByStage) {
  const body = byId("pending-by-stage-body");
  body.replaceChildren();
  const stages = Object.keys(pendingByStage || {});
  if (!stages.length) {
    body.append(element("p", "No pending jobs for your organisation.", "empty-state"));
    return;
  }
  body.append(pendingByStageTable(pendingByStage));
}

async function loadOperationsSummary() {
  const tiles = byId("kpi-tiles");
  tiles.replaceChildren();
  try {
    const summary = await getJson("/v1/operations/summary");
    tiles.append(
      kpiTile("Pending jobs", summary.pending_jobs),
      kpiTile("Oldest pending age", humanizeAge(summary.oldest_pending_age_seconds)),
      kpiTile("Incomplete calls", summary.incomplete_calls, "Not in READY state, including failed and needs-review work."),
    );
    byId("kpi-status").textContent = "Operations summary loaded.";
    renderPendingByStage(summary.pending_by_stage);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      tiles.append(forbiddenTile("Pending jobs"), forbiddenTile("Oldest pending age"), forbiddenTile("Incomplete calls"));
      byId("kpi-status").textContent = "Not available for your role. Administrator access is required for the operations summary.";
    } else {
      tiles.append(failedTile("Pending jobs"), failedTile("Oldest pending age"), failedTile("Incomplete calls"));
      byId("kpi-status").textContent = `Operations summary failed to load · ${error.message}`;
      byId("kpi-status").classList.add("error");
    }
    byId("pending-by-stage-body").replaceChildren(
      element("p", "Pending-by-stage breakdown is unavailable; this is not an all-clear.", "empty-state"),
    );
  }
}

async function loadReviewQueuePanel() {
  const body = byId("queue-panel-body");
  body.replaceChildren();
  try {
    const result = await getJson("/v1/reviews/queue?limit=10");
    const items = result.items || [];
    byId("queue-panel-status").textContent = `${items.length} call${items.length === 1 ? "" : "s"} awaiting review (showing up to 10).`;
    if (!items.length) {
      body.append(element("p", "No unreviewed calls are in the queue.", "empty-state"));
      return;
    }
    const counts = {};
    for (const item of items) counts[item.machine_decision] = (counts[item.machine_decision] || 0) + 1;
    const chartData = Object.entries(counts).map(([label, value]) => ({ label, value }));
    body.append(barChart(chartData, { labelKey: "label", valueKey: "value" }));
    const list = element("ul", undefined, "mini-list");
    for (const item of items.slice(0, 5)) {
      list.append(element("li", `Call ${item.call_id} · ${item.machine_decision} · ${item.processing_state}`));
    }
    body.append(list);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      byId("queue-panel-status").textContent = "Not available for your role. QA analyst or administrator access is required.";
    } else {
      byId("queue-panel-status").textContent = `Review queue failed to load · ${error.message}`;
      byId("queue-panel-status").classList.add("error");
      body.append(element("p", "This panel could not be verified as up to date.", "empty-state"));
    }
  }
}

async function loadLivePanel() {
  const body = byId("live-panel-body");
  body.replaceChildren();
  try {
    const result = await getJson("/v1/live-calls");
    const items = result.items || [];
    byId("live-panel-status").textContent = `${items.length} live or recently changed session${items.length === 1 ? "" : "s"}.`;
    if (!items.length) {
      body.append(element("p", "No live or recently changed sessions.", "empty-state"));
      return;
    }
    const counts = {};
    for (const item of items) {
      const key = item.stale ? "STALE" : item.state;
      counts[key] = (counts[key] || 0) + 1;
    }
    const chartData = Object.entries(counts).map(([label, value]) => ({ label, value }));
    body.append(barChart(chartData, { labelKey: "label", valueKey: "value" }));
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      byId("live-panel-status").textContent = "Not available for your role.";
    } else {
      byId("live-panel-status").textContent = `Live sessions failed to load · ${error.message}`;
      byId("live-panel-status").classList.add("error");
      body.append(element("p", "Live status is stale or unavailable; this is not an all-clear.", "empty-state"));
    }
  }
}

async function loadDispositionPanel() {
  const body = byId("disposition-panel-body");
  body.replaceChildren();
  try {
    const result = await getJson("/v1/disposition-configs");
    const versions = result.versions || [];
    const counts = { ACTIVE: 0, APPROVED: 0, STAGED: 0, RETIRED: 0 };
    for (const version of versions) counts[version.status] = (counts[version.status] || 0) + 1;
    byId("disposition-panel-status").textContent = `${versions.length} configuration version${versions.length === 1 ? "" : "s"} across all configs.`;
    if (!versions.length) {
      body.append(element("p", "No disposition configuration has been staged yet.", "empty-state"));
      return;
    }
    const chartData = Object.entries(counts).filter(([, value]) => value > 0).map(([label, value]) => ({ label, value }));
    body.append(barChart(chartData, { labelKey: "label", valueKey: "value" }));
    const linkRow = element("p", undefined, "panel-link-row");
    linkRow.append(Object.assign(document.createElement("a"), { href: "/dispositions", textContent: "Manage disposition configuration" }));
    body.append(linkRow);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      byId("disposition-panel-status").textContent = "Not available for your role. Administrator access is required.";
    } else {
      byId("disposition-panel-status").textContent = `Disposition configuration failed to load · ${error.message}`;
      byId("disposition-panel-status").classList.add("error");
      body.append(element("p", "This panel could not be verified as up to date.", "empty-state"));
    }
  }
}

async function loadScoresPanel() {
  const body = byId("scores-panel-body");
  body.replaceChildren();
  try {
    const result = await getJson("/v1/me/scores");
    const items = result.items || [];
    byId("scores-panel-status").textContent = `${items.length} of your reviewed call${items.length === 1 ? "" : "s"} loaded.`;
    if (!items.length) {
      body.append(element("p", "No reviewed calls are available yet.", "empty-state"));
      return;
    }
    const scored = items.filter((item) => Number.isFinite(item.reviewed_score));
    const average = scored.length ? scored.reduce((sum, item) => sum + item.reviewed_score, 0) / scored.length : null;
    body.append(kpiTile("Your average reviewed score", average === null ? "Not yet scored" : average.toFixed(2)));
    return;
  } catch (error) {
    if (error.status !== 403) {
      byId("scores-panel-status").textContent = `Your scores failed to load · ${error.message}`;
      byId("scores-panel-status").classList.add("error");
      body.append(element("p", "This panel could not be verified as up to date.", "empty-state"));
      return;
    }
  }
  try {
    const today = new Date();
    const prior = new Date(today.getTime() - 29 * 86400000);
    const params = new URLSearchParams({ start: prior.toISOString(), end: today.toISOString() });
    const report = await getJson(`/v1/reports/team?${params}`);
    const cohorts = report.cohorts || [];
    byId("scores-panel-status").textContent = `${cohorts.length} team cohort${cohorts.length === 1 ? "" : "s"} loaded for the last 30 days.`;
    if (!cohorts.length) {
      body.append(element("p", "No team cohorts were found for this period.", "empty-state"));
      return;
    }
    const chartData = cohorts.filter((cohort) => !cohort.suppressed).map((cohort) => ({ label: cohort.team_id, value: cohort.machine_average || 0 }));
    if (chartData.length) body.append(barChart(chartData, { labelKey: "label", valueKey: "value" }));
    const suppressedCount = cohorts.filter((cohort) => cohort.suppressed).length;
    if (suppressedCount) body.append(element("p", `${suppressedCount} cohort(s) suppressed · fewer than five agents.`, "status"));
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      byId("scores-panel-status").textContent = "Not available for your role. Agent or team leader identity is required.";
    } else {
      byId("scores-panel-status").textContent = `Team report failed to load · ${error.message}`;
      byId("scores-panel-status").classList.add("error");
      body.append(element("p", "This panel could not be verified as up to date.", "empty-state"));
    }
  }
}

function renderCapabilityStatus() {
  const list = byId("capability-list");
  list.replaceChildren();
  const items = [
    { label: "Post-call review and disposition", status: "implemented", detail: "Manual WAV/MP3 upload, redacted final transcript, disposition resolution and QA scoring run with server-verified tests." },
    { label: "Live media provider adapters", status: "pending", detail: "No telephony provider account is connected. The Exotel Stream adapter is a prototype pending authentication and account qualification; other providers in the coverage table are documented but unverified." },
    { label: "Sentiment model", status: "pending", detail: "Sentiment eligibility and windowing logic exist as pure decision helpers; no sentiment model is integrated, so no sentiment values are shown." },
    { label: "Model quality and latency", status: "pending", detail: "No approved production model artifact, calibration dataset, or measured local latency/capacity exists yet. Model confidence remains untrusted until calibrated on adjudicated data." },
    { label: "Live supervisor monitoring", status: "partial", detail: "Live call session state and a short-lived redacted transcript preview are implemented; sentiment alerts, two-leg attribution and browser reconnect behaviour are not." },
    { label: "Operational observability", status: "partial", detail: "A bounded, tenant-scoped operations summary exists; time-series metrics, alerting and spend/capacity telemetry are not implemented." },
  ];
  for (const item of items) {
    const row = element("li", undefined, `capability-row capability-${item.status}`);
    const badge = element("span", item.status.replace("_", " "), "capability-badge");
    row.append(badge, element("strong", item.label), element("p", item.detail));
    list.append(row);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  renderCapabilityStatus();
  loadOperationsSummary();
  loadReviewQueuePanel();
  loadLivePanel();
  loadDispositionPanel();
  loadScoresPanel();
});
