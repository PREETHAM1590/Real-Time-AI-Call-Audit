"use strict";

const byId = (id) => document.getElementById(id);
const SAMPLE_CONFIG = {
  schema_version: "1.0.0",
  config_id: "retention_v1",
  version: 1,
  identity: { use_case_id: "retention", display_name: "Synthetic retention disposition" },
  questions: {
    committed: { type: "noul", instructions: "Was a commitment made?", criteria: { true: "Yes", false: "No" } },
    next_step: { type: "choice", instructions: "Select the next step", criteria: { CALLBACK: "Callback", DECLINED: "Declined" } },
  },
  taxonomy: {
    codes: [
      { code: "COMMITTED", name: "Committed", level: 0, parent_code: null, terminal: true, callback: false, success: true },
      { code: "REVIEW", name: "Review", level: 0, parent_code: null, terminal: true, callback: false, success: false },
      { code: "CALLBACK", name: "Callback", level: 0, parent_code: null, terminal: true, callback: true, success: false },
      { code: "DECLINED", name: "Declined", level: 0, parent_code: null, terminal: true, callback: false, success: false },
      { code: "NO_CONTACT", name: "No contact", level: 0, parent_code: null, terminal: true, callback: true, success: false },
    ],
  },
  front_gates: [{ id: "NO_CONNECT", enabled: true, priority: 1, source: "telephony_status", condition: { operator: "IN", value: ["NO_ANSWER"] }, emit: "NO_CONTACT" }],
  resolver: {
    strategy: "FIRST_MATCH_WINS",
    default_emit: "REVIEW",
    rules: [
      { id: "COMMIT", priority: 10, when: { all: [{ signal: "committed", operator: "NOUL_GTE", value: 0.7 }] }, emit: "COMMITTED" },
      { id: "CALLBACK", priority: 20, when: { all: [{ signal: "next_step", operator: "CHOICE_EQ", value: "CALLBACK" }] }, emit: "CALLBACK" },
    ],
  },
  confidence: { default_commit_threshold: 0.6, default_review_threshold: 0.4, low_confidence_action: "REVIEW" },
  runtime: { context_limit_chars: 2000, window_chars: 1000, overlap_turns: 0, max_chunks: 4 },
};

let versions = [];

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (typeof options.body === "string") headers.set("Content-Type", "application/json");
  if ((options.method || "GET") !== "GET") {
    const csrf = await fetch("/v1/csrf", { credentials: "include", headers: { Accept: "application/json" } });
    if (!csrf.ok) throw Object.assign(new Error("Your session could not be verified. Sign in again and reload."), { status: csrf.status });
    headers.set("X-CSRF-Token", (await csrf.json()).csrf_token);
  }
  const response = await fetch(path, { ...options, credentials: "include", headers });
  let payload = {};
  if (response.status !== 204) {
    try { payload = await response.json(); } catch { /* use status below */ }
  }
  if (!response.ok) {
    const message = typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status})`;
    throw Object.assign(new Error(message), { status: response.status, detail: payload.detail });
  }
  return payload;
}

function setStatus(id, message, error = false) {
  const node = byId(id);
  node.textContent = message;
  node.classList.toggle("error", error);
}

function statusPill(status) {
  const pill = element("span", status.replace("_", " "), `status-pill pill-${status.toLowerCase()}`);
  return pill;
}

function renderVersions() {
  const body = byId("versions-body");
  body.replaceChildren();
  for (const version of versions) {
    const row = document.createElement("tr");
    row.append(
      element("td", version.config_id),
      element("td", `v${version.version}`),
    );
    const statusCell = document.createElement("td");
    statusCell.append(statusPill(version.status));
    row.append(statusCell);
    row.append(
      element("td", version.content_hash.slice(0, 12)),
      element("td", version.created_by),
      element("td", version.approved_by || "Not yet approved"),
    );
    const actionsCell = document.createElement("td");
    const actions = element("div", undefined, "row-actions");
    if (version.status === "STAGED") {
      const approve = element("button", "Approve");
      approve.type = "button";
      approve.addEventListener("click", () => approveVersion(version));
      actions.append(approve);
    }
    if (version.status === "APPROVED") {
      const activate = element("button", "Activate");
      activate.type = "button";
      activate.addEventListener("click", () => activateVersion(version));
      actions.append(activate);
    }
    const replay = element("button", "Replay synthetic fixture");
    replay.type = "button";
    replay.addEventListener("click", () => replayVersion(version));
    actions.append(replay);
    actionsCell.append(actions);
    row.append(actionsCell);
    body.append(row);
  }
  if (!versions.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "empty-state";
    cell.textContent = "No configuration versions have been staged for this organisation.";
    row.append(cell);
    body.append(row);
  }
}

async function loadVersions() {
  setStatus("dispositions-status", "Loading configuration versions…");
  try {
    const result = await api("/v1/disposition-configs");
    versions = result.versions || [];
    renderVersions();
    byId("dispositions-admin").hidden = false;
    setStatus("dispositions-status", `${versions.length} configuration version${versions.length === 1 ? "" : "s"} loaded.`);
  } catch (error) {
    byId("dispositions-admin").hidden = true;
    if (error.status === 401 || error.status === 403) {
      setStatus("dispositions-status", "Not available for your role. Administrator access is required to manage disposition configuration.", true);
    } else {
      setStatus("dispositions-status", `Configuration versions could not be loaded · ${error.message}. This is a failure state, not an empty configuration.`, true);
    }
  }
}

async function approveVersion(version) {
  const reason = window.prompt(`Reason for approving ${version.config_id} v${version.version}:`, "");
  if (reason === null) return;
  if (!reason.trim()) {
    setStatus("action-status", "An approval reason is required.", true);
    return;
  }
  try {
    await api(`/v1/disposition-configs/${encodeURIComponent(version.config_id)}/versions/${version.version}/approve`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
    setStatus("action-status", `Version ${version.version} of ${version.config_id} approved.`);
    await loadVersions();
  } catch (error) { reportActionError(error, version); }
}

async function activateVersion(version) {
  try {
    await api(`/v1/disposition-configs/${encodeURIComponent(version.config_id)}/versions/${version.version}/activate`, {
      method: "POST",
      body: JSON.stringify({ expected_generation: version.active_generation || 0 }),
    });
    setStatus("action-status", `Version ${version.version} of ${version.config_id} activated.`);
    await loadVersions();
  } catch (error) { reportActionError(error, version); }
}

async function replayVersion(version) {
  try {
    const result = await api(`/v1/disposition-configs/${encodeURIComponent(version.config_id)}/versions/${version.version}/replay`, {
      method: "POST",
      body: JSON.stringify({ fixture_set_id: "synthetic-smoke-v1" }),
    });
    setStatus("action-status", `Replay of ${version.config_id} v${version.version}: ${result.resolved_count} resolved, ${result.review_count} needs review.`);
  } catch (error) { reportActionError(error, version); }
}

function reportActionError(error, version) {
  if (error.status === 409) {
    setStatus("action-status", `Conflict for ${version.config_id} v${version.version} · ${error.message}. Reloading current state.`, true);
    loadVersions();
  } else if (error.status === 403) {
    setStatus("action-status", "Not available for your role. Administrator access is required.", true);
  } else if (error.status === 422 && Array.isArray(error.detail)) {
    setStatus("action-status", `Invalid request for ${version.config_id} v${version.version} · ${error.detail.map((item) => `${item.path}: ${item.message}`).join("; ")}`, true);
  } else {
    setStatus("action-status", error.message, true);
  }
}

function renderFieldErrors(errors) {
  const host = byId("field-errors");
  host.replaceChildren();
  if (!errors?.length) return;
  const list = document.createElement("ul");
  for (const item of errors) list.append(element("li", `${item.path}: ${item.message}`));
  host.append(element("p", "This configuration is invalid:"), list);
}

byId("load-sample").addEventListener("click", () => {
  byId("config-json").value = JSON.stringify(SAMPLE_CONFIG, null, 2);
  renderFieldErrors([]);
});

byId("validate-config").addEventListener("click", async () => {
  renderFieldErrors([]);
  let parsed;
  try {
    parsed = JSON.parse(byId("config-json").value);
  } catch {
    setStatus("action-status", "The configuration is not valid JSON.", true);
    return;
  }
  try {
    const result = await api("/v1/disposition-configs/validate", { method: "POST", body: JSON.stringify(parsed) });
    setStatus("action-status", `Valid: ${result.config_id} v${result.version} · ${result.question_count} question(s) · taxonomy codes: ${result.taxonomy_codes.join(", ")}`);
  } catch (error) {
    if (error.status === 422 && Array.isArray(error.detail)) {
      renderFieldErrors(error.detail);
      setStatus("action-status", "Validation failed. See field errors below.", true);
    } else {
      setStatus("action-status", error.message, true);
    }
  }
});

byId("stage-config").addEventListener("click", async () => {
  renderFieldErrors([]);
  let parsed;
  try {
    parsed = JSON.parse(byId("config-json").value);
  } catch {
    setStatus("action-status", "The configuration is not valid JSON.", true);
    return;
  }
  try {
    const result = await api("/v1/disposition-configs", { method: "POST", body: JSON.stringify(parsed) });
    setStatus("action-status", `Staged ${result.config_id} v${result.version}. It still requires approval by a different administrator before activation.`);
    await loadVersions();
  } catch (error) {
    if (error.status === 422 && Array.isArray(error.detail)) {
      renderFieldErrors(error.detail);
      setStatus("action-status", "Staging failed. See field errors below.", true);
    } else if (error.status === 409) {
      setStatus("action-status", `Conflict · ${error.message}`, true);
    } else if (error.status === 403) {
      setStatus("action-status", "Not available for your role. Administrator access is required.", true);
    } else {
      setStatus("action-status", error.message, true);
    }
  }
});

byId("rollback-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const configId = byId("rollback-config-id").value.trim();
  const version = Number(byId("rollback-version").value);
  const expectedGeneration = Number(byId("rollback-generation").value);
  const reason = byId("rollback-reason").value;
  try {
    await api(`/v1/disposition-configs/${encodeURIComponent(configId)}/rollback`, {
      method: "POST",
      body: JSON.stringify({ version, expected_generation: expectedGeneration, reason }),
    });
    setStatus("action-status", `Rolled back ${configId} to version ${version}.`);
    event.currentTarget.reset();
    await loadVersions();
  } catch (error) {
    if (error.status === 409) {
      setStatus("action-status", `Conflict · ${error.message}. Reload to see the current active version and generation.`, true);
    } else if (error.status === 422 && Array.isArray(error.detail)) {
      setStatus("action-status", error.detail.map((item) => `${item.path}: ${item.message}`).join("; "), true);
    } else if (error.status === 403) {
      setStatus("action-status", "Not available for your role. Administrator access is required.", true);
    } else {
      setStatus("action-status", error.message, true);
    }
  }
});

byId("refresh-versions").addEventListener("click", loadVersions);

loadVersions();
