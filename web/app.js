"use strict";

const dimensions = ["greeting", "listening", "resolution", "compliance", "clarity", "objection", "closing"];
const state = { queue: [], call: null, callId: null, selectedEvidence: null, audioGranted: false };
const byId = (id) => document.getElementById(id);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function timestamp(ms) {
  const value = Number.isFinite(ms) && ms >= 0 ? Math.floor(ms / 1000) : 0;
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if ((options.method || "GET") !== "GET") {
    const csrf = await fetch("/v1/csrf", { credentials: "include", headers: { Accept: "application/json" } });
    if (!csrf.ok) throw new Error("Your session could not be verified. Sign in again and reload this page.");
    headers.set("X-CSRF-Token", (await csrf.json()).csrf_token);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "include",
  });
  let payload = {};
  try { payload = await response.json(); } catch { /* safe status text below */ }
  if (!response.ok) {
    const error = new Error(payload.detail || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function renderQueue() {
  const list = byId("queue-list");
  list.replaceChildren();
  for (const item of state.queue) {
    const row = element("li");
    const label = `Call ${item.call_id}, ${item.machine_decision}, machine score ${item.machine_score ?? "unavailable"}`;
    const button = element("button", label);
    button.type = "button";
    button.setAttribute("aria-current", String(item.call_id === state.callId));
    button.addEventListener("click", () => loadCall(item.call_id));
    row.append(button);
    list.append(row);
  }
  if (!state.queue.length) byId("queue-status").textContent = "No unreviewed calls are in the queue.";
}

async function loadQueue() {
  byId("queue-status").textContent = "Loading review queue…";
  try {
    const result = await request("/v1/reviews/queue?limit=50");
    state.queue = result.items || [];
    renderQueue();
    byId("queue-status").textContent = `${state.queue.length} call${state.queue.length === 1 ? "" : "s"} loaded.`;
  } catch (error) {
    byId("queue-status").textContent = error.message;
    byId("queue-status").classList.add("error");
  }
}

function addEvidenceButton(container, evidence, byUtterance) {
  const utterance = byUtterance.get(evidence.utterance_id);
  if (!utterance) return;
  const button = element("button", `Open ${utterance.role} evidence at ${timestamp(utterance.start_ms)}`, "evidence-link");
  button.type = "button";
  button.addEventListener("click", () => {
    const selected = byUtterance.get(evidence.utterance_id);
    const target = selected.node;
    target.focus();
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    if (state.audioGranted) {
      const player = byId("call-audio");
      player.currentTime = selected.start_ms / 1000;
      player.play().catch(() => {});
    }
    byId("call-status").textContent = `Evidence selected at ${timestamp(utterance.start_ms)}.`;
  });
  container.append(button);
}

function renderCall(data) {
  state.call = data;
  state.audioGranted = false;
  const host = byId("call-detail");
  host.replaceChildren();
  const call = data.call;
  const audit = data.audit;
  byId("call-title").textContent = `Call ${call.id}`;
  const summary = element("div", undefined, "summary-grid");
  const summaries = [
    ["Processing state", call.processing_state],
    ["Agent", call.agent_id],
    ["Team", call.team_id],
    ["Call date", call.created_at],
    ["Transcript revision", call.transcript_revision],
    ["Machine score", audit?.machine_score ?? "Unavailable"],
    ["Machine decision", audit?.machine_decision ?? "Unavailable"],
    ["Reviewed score", data.reviews.length ? (data.reviews[data.reviews.length - 1].effective_score ?? "Unscored") : "Not reviewed"],
    ["Disposition", data.disposition ? `${data.disposition.code || data.disposition.status} · config v${data.disposition.config_version}` : "Unavailable for this transcript revision"],
  ];
  for (const [title, value] of summaries) {
    const card = element("div", undefined, "summary-card");
    card.append(element("strong", title), element("span", value));
    summary.append(card);
  }
  host.append(summary);

  const transcriptBlock = element("section", undefined, "block");
  transcriptBlock.append(element("h3", "Final redacted transcript"));
  const utteranceIndex = new Map();
  for (const row of data.transcript) {
    const node = element("article", undefined, "transcript-row");
    node.tabIndex = -1;
    node.append(element("span", `${row.role} · ${timestamp(row.start_ms)}–${timestamp(row.end_ms)}`, "role"));
    node.append(element("p", row.text_redacted));
    utteranceIndex.set(row.id, { ...row, node });
    transcriptBlock.append(node);
  }
  host.append(transcriptBlock);

  if (audit) {
    const dimensionBlock = element("section", undefined, "block");
    dimensionBlock.append(element("h3", `Machine audit · ${audit.rubric_version} · ${audit.machine_decision}${audit.superseded ? " · superseded" : ""}`));
    const dimensionsHost = element("div", undefined, "dimension-list");
    for (const dimension of audit.dimensions || []) {
      const card = element("article", undefined, "dimension");
      card.append(element("h4", `${dimension.id}: ${dimension.score ?? dimension.status}`));
      card.append(element("p", dimension.reason || "No model reason stored."));
      const links = element("div", undefined, "evidence-links");
      for (const citation of dimension.evidence || []) addEvidenceButton(links, citation, utteranceIndex);
      if (!links.childElementCount) links.append(element("span", "No AGENT evidence cited."));
      card.append(links);
      dimensionsHost.append(card);
    }
    dimensionBlock.append(dimensionsHost);
    host.append(dimensionBlock);
  }

  if (data.findings?.length) {
    const findingsBlock = element("section", undefined, "block");
    findingsBlock.append(element("h3", "Policy findings"));
    const rows = element("div", undefined, "finding-list");
    for (const finding of data.findings) rows.append(element("article", `${finding.rule_id} · ${finding.status} · ${finding.severity}: ${finding.remediation}`, "finding"));
    findingsBlock.append(rows);
    host.append(findingsBlock);
  }
  if (data.reviews?.length) {
    const reviewBlock = element("section", undefined, "block");
    reviewBlock.append(element("h3", "Human review history"));
    const rows = element("div", undefined, "review-list");
    for (const review of data.reviews) rows.append(element("article", `Review ${review.version} · ${review.action} · ${review.effective_decision} · ${review.reason}`, "review-entry"));
    reviewBlock.append(rows);
    host.append(reviewBlock);
  }
  const playback = element("section", undefined, "block");
  playback.append(element("h3", "Call audio"));
  const accessButton = element("button", "Enable authorised audio playback");
  let player = null;
  accessButton.type = "button";
  accessButton.addEventListener("click", async () => {
    accessButton.disabled = true;
    try {
      const grant = await request(`/v1/calls/${encodeURIComponent(call.id)}/audio-access`, { method: "POST" });
      if (!player) {
        player = element("audio");
        player.id = "call-audio";
        player.controls = true;
        player.preload = "metadata";
        player.setAttribute("aria-label", "Authorised call audio");
        playback.append(player);
      }
      player.src = grant.url;
      state.audioGranted = true;
      accessButton.textContent = "Renew audio access";
      accessButton.disabled = false;
      byId("call-status").textContent = "Audio access granted and recorded. Evidence links can seek within this call.";
    } catch (error) {
      accessButton.disabled = false;
      byId("call-status").textContent = error.message;
      byId("call-status").classList.add("error");
    }
  });
  playback.append(accessButton, element("p", "Playback access is recorded before a 60-second, user-bound audio URL is issued. Renew access before expiry to continue listening.", "status"));
  host.append(playback);
  const alreadyTriaged = data.reviews?.some((review) => review.action === "TRIAGE" && review.effective_decision === "NEEDS_REVIEW");
  if (audit && !audit.superseded && alreadyTriaged) {
    const triageStatus = element("p", "This call has been triaged and remains in the review queue. Its evidence is still insufficient for a score.", "status");
    host.append(triageStatus);
  } else if (audit && !audit.superseded) renderReviewForm(host, audit, data);
  else if (audit?.superseded) byId("call-status").textContent = "This audit uses superseded transcript evidence. Scoring actions are disabled; reload the current review queue.";
}

function renderReviewForm(host, audit, data) {
  const form = element("form", undefined, "review-form");
  form.append(element("h3", audit.machine_decision === "NEEDS_REVIEW" ? "Record review triage" : "Review machine result"));
  const baseVersion = data.current_review_version || 0;
  const inputs = new Map();
  if (audit.machine_decision !== "NEEDS_REVIEW" && audit.machine_score !== null) {
    const scoreFields = element("div", undefined, "score-fields");
    for (const dimension of dimensions) {
      const wrapper = element("div", undefined, "score-field");
      const label = element("label", `${dimension} override`);
      label.htmlFor = `score-${dimension}`;
      const select = element("select");
      select.id = `score-${dimension}`;
      select.name = dimension;
      select.append(new Option("No change", ""));
      for (let score = 1; score <= 5; score += 1) select.append(new Option(String(score), String(score)));
      inputs.set(dimension, select);
      wrapper.append(label, select);
      scoreFields.append(wrapper);
    }
    form.append(scoreFields);
  }
  const reasonLabel = element("label", "Review reason");
  reasonLabel.htmlFor = "review-reason";
  const reason = element("textarea");
  reason.id = "review-reason";
  reason.name = "reason";
  reason.required = true;
  reason.maxLength = 1000;
  reasonLabel.append(reason);
  form.append(reasonLabel);
  const actions = element("div", undefined, "actions");
  if (audit.machine_decision === "NEEDS_REVIEW" || audit.machine_score === null) {
    const triage = element("button", "Triage and keep in review");
    triage.type = "submit";
    triage.value = "TRIAGE";
    actions.append(triage);
  } else {
    const accept = element("button", "Accept current score");
    accept.type = "submit";
    accept.value = "ACCEPT";
    const override = element("button", "Save score changes");
    override.type = "submit";
    override.value = "OVERRIDE";
    actions.append(accept, override);
  }
  form.append(actions);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const action = event.submitter?.value;
    const changed = {};
    if (action === "OVERRIDE") {
      for (const [dimension, select] of inputs) if (select.value) changed[dimension] = Number(select.value);
      if (!Object.keys(changed).length) {
        byId("call-status").textContent = "Choose at least one score to override.";
        byId("call-status").classList.add("error");
        return;
      }
    }
    const buttons = [...actions.querySelectorAll("button")];
    buttons.forEach((button) => { button.disabled = true; });
    byId("call-status").textContent = "Saving review…";
    try {
      await request(`/v1/audits/${encodeURIComponent(audit.id)}/reviews`, {
        method: "POST",
        body: JSON.stringify({ action, base_review_version: baseVersion, scores: changed, reason: reason.value }),
      });
      await loadQueue();
      await loadCall(state.callId);
      byId("call-status").textContent = "Review saved.";
      byId("call-title").focus();
    } catch (error) {
      if (error.status === 409) {
        await loadQueue();
        await loadCall(state.callId);
        byId("call-status").textContent = "The review changed. Reload the latest call evidence before saving again.";
      } else {
        byId("call-status").textContent = error.message;
        buttons.forEach((button) => { button.disabled = false; });
      }
      byId("call-status").classList.add("error");
    }
  });
  host.append(form);
}

async function loadCall(callId) {
  state.callId = callId;
  state.audioGranted = false;
  byId("call-status").classList.remove("error");
  byId("call-status").textContent = "Loading call evidence…";
  try {
    const detail = await request(`/v1/calls/${encodeURIComponent(callId)}`);
    renderCall(detail);
    byId("call-status").textContent = detail.audit?.superseded
      ? "This audit uses superseded transcript evidence. Scoring actions are disabled; reload the current review queue."
      : "Current final redacted evidence loaded.";
    renderQueue();
  } catch (error) {
    byId("call-status").textContent = error.message;
    byId("call-status").classList.add("error");
  }
}

byId("refresh-queue").addEventListener("click", loadQueue);
document.addEventListener("DOMContentLoaded", loadQueue);
