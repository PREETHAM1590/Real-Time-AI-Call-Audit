"use strict";

const byId = (id) => document.getElementById(id);
let integrations = [];
let credential = null;

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
    try { payload = await response.json(); } catch { /* use the status below */ }
  }
  if (!response.ok) throw Object.assign(new Error(payload.detail || `Request failed (${response.status})`), { status: response.status });
  return payload;
}

function status(message, error = false) {
  const node = byId("exotel-status");
  node.textContent = message;
  node.classList.toggle("error", error);
}

function clearCredential() {
  credential = null;
  byId("exotel-username").textContent = "";
  byId("exotel-password").textContent = "";
  byId("exotel-copy-status").textContent = "";
  byId("exotel-secret").hidden = true;
}

async function loadIntegrations(keepSelected = true) {
  const previous = keepSelected ? byId("exotel-integration-select").value : "";
  const result = await api("/v1/exotel-integrations");
  integrations = result.integrations || [];
  const select = byId("exotel-integration-select");
  select.replaceChildren(new Option(integrations.length ? "Choose an integration" : "No integrations configured", ""));
  for (const item of integrations) select.add(new Option(`${item.account_sid} · ${item.status}`, item.id));
  select.value = integrations.some((item) => item.id === previous) ? previous : (integrations[0]?.id || "");

  const list = byId("exotel-integration-list");
  list.replaceChildren();
  for (const item of integrations) {
    const row = document.createElement("li");
    const detail = document.createElement("span");
    detail.textContent = `${item.account_sid} · ${item.username} · ${item.status}`;
    row.append(detail);
    if (item.status === "ACTIVE") {
      const disable = document.createElement("button");
      disable.type = "button";
      disable.textContent = "Disable";
      disable.addEventListener("click", () => disableIntegration(item));
      row.append(disable);
    }
    list.append(row);
  }
  await loadMappings();
}

async function loadMappings() {
  const id = byId("exotel-integration-select").value;
  const panel = byId("exotel-mappings");
  panel.hidden = !id;
  const list = byId("exotel-mapping-list");
  list.replaceChildren();
  if (!id) return;
  const result = await api(`/v1/exotel-integrations/${encodeURIComponent(id)}/agents`);
  const selected = integrations.find((item) => item.id === id);
  byId("exotel-map-form").hidden = selected?.status !== "ACTIVE";
  for (const mapping of result.mappings || []) {
    const row = document.createElement("li");
    const detail = document.createElement("span");
    detail.textContent = `${mapping.agent_ref} maps to ${mapping.agent_id} in ${mapping.team_id}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeMapping(id, mapping.agent_ref));
    row.append(detail, remove);
    list.append(row);
  }
  if (!list.childElementCount) list.append(Object.assign(document.createElement("li"), { textContent: "No agent mappings yet." }));
}

async function disableIntegration(item) {
  try {
    await api(`/v1/exotel-integrations/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    status(`Integration ${item.account_sid} disabled.`);
    await loadIntegrations();
  } catch (error) { showError(error); }
}

async function removeMapping(id, ref) {
  try {
    await api(`/v1/exotel-integrations/${encodeURIComponent(id)}/agents/${encodeURIComponent(ref)}`, { method: "DELETE" });
    status(`Mapping ${ref} removed.`);
    await loadMappings();
  } catch (error) { showError(error); }
}

function showError(error) {
  status(error.status === 401 || error.status === 403
    ? "Administrator access or a valid sign-in is required. Setup controls are hidden."
    : error.message, true);
  if (error.status === 401 || error.status === 403) {
    byId("exotel-admin").hidden = true;
    clearCredential();
  }
}

byId("exotel-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const result = await api("/v1/exotel-integrations", {
      method: "POST",
      body: JSON.stringify({ account_sid: byId("exotel-account-sid").value }),
    });
    byId("exotel-account-sid").value = "";
    credential = { username: result.username, password: result.password };
    byId("exotel-username").textContent = credential.username;
    byId("exotel-password").textContent = credential.password;
    byId("exotel-secret").hidden = false;
    status("Integration created. Save the one-time password before dismissing it.");
    try { await loadIntegrations(false); }
    catch (error) {
      status(`Integration created; save its one-time password. The integration list could not refresh · ${error.message}`, true);
    }
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
});

byId("exotel-map-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const id = byId("exotel-integration-select").value;
  const ref = byId("exotel-agent-ref").value.trim();
  try {
    await api(`/v1/exotel-integrations/${encodeURIComponent(id)}/agents/${encodeURIComponent(ref)}`, {
      method: "PUT",
      body: JSON.stringify({ agent_id: byId("exotel-agent-id").value, team_id: byId("exotel-team-id").value }),
    });
    form.reset();
    status(`Mapping ${ref} saved.`);
    await loadMappings();
  } catch (error) { showError(error); }
});

byId("exotel-integration-select").addEventListener("change", () => loadMappings().catch(showError));
byId("exotel-secret-dismiss").addEventListener("click", clearCredential);
byId("exotel-secret").addEventListener("click", async (event) => {
  const kind = event.target.dataset.copy;
  if (!kind || !credential) return;
  try {
    await navigator.clipboard.writeText(credential[kind]);
    byId("exotel-copy-status").textContent = `${kind === "username" ? "Username" : "Password"} copied.`;
  } catch {
    byId("exotel-copy-status").textContent = "Clipboard access was unavailable. Select and copy the displayed value, then dismiss it.";
  }
});

loadIntegrations(false).then(() => {
  byId("exotel-admin").hidden = false;
}).then(() => status("Admin setup loaded. Exotel remains a prototype pending protocol qualification."))
  .catch((error) => {
    if (error.status === 401 || error.status === 403) showError(error);
    else status(`Exotel setup could not be loaded · ${error.message}`, true);
  });
