"use strict";

const dimensions = ["greeting", "listening", "resolution", "compliance", "clarity", "objection", "closing"];
const MAX_UPLOAD_BYTES = 250 * 1024 * 1024;
const EVENTS_INITIAL_RETRY_MS = 500;
const EVENTS_MAX_RETRY_MS = 8_000;
const state = { queue: [], call: null, callId: null, selectedEvidence: null, audioGranted: false, uploadKey: null, lastAppliedEventSequence: 0, eventsCursor: 0 };
let liveRefreshActive = false;
let eventsStarted = false;
let eventsAbortController = null;
let eventsReconnectTimer = null;
let eventsRetryDelayMs = EVENTS_INITIAL_RETRY_MS;
const liveCallNodes = new Map();
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

function age(createdAt) {
  const created = Date.parse(createdAt);
  if (!Number.isFinite(created)) return "Age unavailable";
  const minutes = Math.max(0, Math.floor((Date.now() - created) / 60000));
  if (minutes < 60) return `${minutes}m old`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h old`;
  return `${Math.floor(hours / 24)}d old`;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (typeof options.body === "string") headers.set("Content-Type", "application/json");
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
  byId("queue-order").textContent = "Server order: NEEDS_REVIEW, then FAIL, then other decisions; newest calls first within each group.";
  for (const item of state.queue) {
    const row = element("li");
    const label = `Call ${item.call_id}, ${item.machine_decision}, machine score ${item.machine_score ?? "unavailable"}`;
    const button = element("button", label);
    button.type = "button";
    button.setAttribute("aria-current", String(item.call_id === state.callId));
    const context = element("span", undefined, "queue-context");
    context.append(
      element("span", `Decision: ${item.machine_decision}`),
      element("span", `Processing: ${item.processing_state}`),
      element("span", `Age: ${age(item.created_at)}`),
      element("span", `Agent: ${item.agent_id}`),
      element("span", `Team: ${item.team_id}`),
    );
    button.append(context);
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

async function loadLiveCalls() {
  if (liveRefreshActive) return;
  liveRefreshActive = true;
  const status = byId("live-status");
  const list = byId("live-calls-list");
  status.textContent = "Loading live sessions…";
  status.classList.remove("error");
  try {
    const result = await request("/v1/live-calls");
    const calls = (result.items || []).slice(0, 10);
    const currentKeys = new Set(calls.map((call) => call.call_key));
    for (const [key, node] of liveCallNodes) {
      if (!currentKeys.has(key)) {
        node.remove();
        liveCallNodes.delete(key);
      }
    }
    const transcriptLoads = [];
    for (const call of calls) {
      let item = liveCallNodes.get(call.call_key);
      if (!item) {
        item = element("li", undefined, "live-call");
        const sessionLabel = element("strong", undefined, "live-session-status");
        const callKey = element("span");
        const membership = element("span");
        const started = element("span");
        const sentimentNote = element("span", "Sentiment: not integrated", "live-capability-note");
        const policyNote = element("span", "Policy alerts: not evaluated live", "live-capability-note");
        const transcriptStatus = element("span", "", "live-transcript-status");
        const utterances = element("ol", undefined, "live-utterances");
        item.append(sessionLabel, callKey, membership, started, sentimentNote, policyNote, transcriptStatus, utterances);
        item._liveNodes = { sessionLabel, callKey, membership, started, transcriptStatus, utterances };
        liveCallNodes.set(call.call_key, item);
      }
      item.dataset.callKey = call.call_key;
      const nodes = item._liveNodes;
      item.className = `live-call live-${String(call.state).toLowerCase()}${call.stale ? " live-stale" : ""}`;
      nodes.sessionLabel.textContent = call.stale ? "STALE · last media update is old" : ({
        LIVE: "LIVE · audio session connected",
        DRAINING: "DRAINING · waiting for recording intake",
        ENDED: "ENDED · recording intake accepted",
        INCOMPLETE: "INCOMPLETE · stream ended before intake was accepted",
      }[call.state] || "UNKNOWN · session state unavailable");
      nodes.callKey.textContent = `Call key ${call.call_key}`;
      nodes.membership.textContent = `Agent ${call.agent_id} · Team ${call.team_id}`;
      nodes.started.textContent = `Started ${new Date(call.started_at).toLocaleString()}`;
      if (item.parentElement !== list) list.append(item);
      transcriptLoads.push(loadLiveTranscript(call, nodes.transcriptStatus, nodes.utterances));
    }
    status.textContent = `${calls.length} live or recently changed session${calls.length === 1 ? "" : "s"}${(result.items || []).length > calls.length ? `; showing the latest ${calls.length}` : ""}.`;
    if (!list.childElementCount) list.append(element("li", "No live or recently changed sessions.", "empty-state"));
    else if (list.querySelector(".empty-state")) list.querySelector(".empty-state").remove();
    await Promise.allSettled(transcriptLoads);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      for (const [key, node] of liveCallNodes) {
        node.remove();
        liveCallNodes.delete(key);
      }
      list.replaceChildren(element("li", "Cached live session details were cleared.", "empty-state"));
      status.textContent = "Live sessions hidden · sign-in or authorization is required.";
    } else {
      for (const node of liveCallNodes.values()) {
        node.classList.add("live-stale");
        node._liveNodes.sessionLabel.textContent = "STALE · live status unavailable";
        node._liveNodes.utterances.replaceChildren();
        node._liveNodes.transcriptStatus.textContent = "Live preview cleared · showing cached session labels only.";
      }
      status.textContent = `Live status temporarily unavailable · ${error.message}`;
    }
    status.classList.add("error");
  } finally {
    liveRefreshActive = false;
  }
}

async function loadLiveTranscript(call, status, list) {
  const label = {
    DISABLED: "Live transcription disabled · recording intake continues.",
    EMPTY: "Live transcription enabled · waiting for redacted utterances.",
    LIVE: "Redacted live preview · provisional only.",
    DEGRADED: "Live transcription degraded · recording intake continues.",
  };
  try {
    const result = await request(`/v1/live-calls/${encodeURIComponent(call.call_key)}/utterances?generation=${encodeURIComponent(call.generation)}`);
    status.textContent = label[result.status] || "Live transcript status unavailable.";
    if (result.truncated) status.textContent += " Older preview utterances were dropped; the post-call transcript is authoritative.";
    list.replaceChildren();
    for (const utterance of result.items || []) {
      const row = element("li", undefined, "live-utterance");
      row.append(element("time", `${timestamp(utterance.start_ms)} · ${utterance.role}`), element("span", utterance.text_redacted));
      list.append(row);
    }
    if (!list.childElementCount && result.status === "LIVE") status.textContent = "Live transcript was published and has since expired from the short-lived preview.";
  } catch {
    status.textContent = "Live transcript unavailable · session scope, expiry, or storage may have changed.";
    list.replaceChildren();
  }
}

function addEvidenceButton(container, evidence, byUtterance) {
  const utterance = byUtterance.get(evidence.utterance_id);
  if (!utterance) return;
  const quote = typeof evidence.quote === "string" ? evidence.quote : utterance.text_redacted;
  const button = element("button", `“${quote}” · Open ${utterance.role} evidence at ${timestamp(utterance.start_ms)}`, "evidence-link");
  button.setAttribute("aria-label", `Open ${utterance.role} evidence “${quote}” at ${timestamp(utterance.start_ms)}`);
  button.type = "button";
  button.addEventListener("click", () => {
    const selected = byUtterance.get(evidence.utterance_id);
    const target = selected.node;
    for (const prior of document.querySelectorAll(".transcript-row mark")) {
      prior.replaceWith(document.createTextNode(prior.textContent));
    }
    const paragraph = target.querySelector("p");
    const text = selected.text_redacted;
    const start = text.indexOf(quote);
    paragraph.replaceChildren();
    if (start >= 0 && quote) {
      paragraph.append(document.createTextNode(text.slice(0, start)));
      paragraph.append(element("mark", quote, "selected-evidence"), document.createTextNode(text.slice(start + quote.length)));
    } else paragraph.textContent = text;
    target.focus();
    target.scrollIntoView({ behavior: "smooth", block: matchMedia("(max-width: 860px)").matches ? "start" : "center" });
    if (state.audioGranted) {
      const player = byId("call-audio");
      player.currentTime = selected.start_ms / 1000;
      player.play().catch(() => {});
    }
    byId("call-status").textContent = `Evidence selected at ${timestamp(utterance.start_ms)}.`;
  });
  container.append(button);
}

function renderDispositionCard(host, disposition) {
  const block = element("section", undefined, "block");
  block.append(element("h3", "Disposition (separate from QA audit score)"));
  if (!disposition) {
    block.append(element("p", "Unavailable for this transcript revision.", "empty-state"));
    host.append(block);
    return;
  }
  const card = element("div", undefined, "disposition-card");
  const fields = [
    ["Code", disposition.code || disposition.status || "Unavailable"],
    ["Status", disposition.status ?? "Unavailable"],
    ["Matched rule", disposition.matched_rule_id ?? "None"],
    ["Processing path", disposition.processing_path ?? "Unavailable"],
    ["Confidence", disposition.confidence ?? "Unavailable"],
    ["Requires review", disposition.requires_review === undefined ? "Unavailable" : (disposition.requires_review ? "Yes" : "No")],
    ["Review reason", disposition.review_reason ?? "None"],
    ["Config", disposition.config_id ? `${disposition.config_id} · v${disposition.config_version}` : "Unavailable"],
    ["Config hash", disposition.config_hash ? disposition.config_hash.slice(0, 12) : "Unavailable"],
    ["Model artifact", disposition.model_artifact ?? "Unavailable"],
    ["Adapter version", disposition.adapter_version ?? "Unavailable"],
    ["Transcript revision", disposition.transcript_revision ?? "Unavailable"],
    ["Revision", disposition.revision ?? "Unavailable"],
    ["Created at", disposition.created_at ?? "Unavailable"],
  ];
  for (const [title, value] of fields) {
    const cell = element("div", undefined, "summary-card");
    cell.append(element("strong", title), element("span", value));
    card.append(cell);
  }
  block.append(card);
  host.append(block);
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
  ];
  const summaryFieldIds = { "Processing state": "summary-processing-state", "Transcript revision": "summary-transcript-revision" };
  for (const [title, value] of summaries) {
    const card = element("div", undefined, "summary-card");
    const valueNode = element("span", value);
    if (summaryFieldIds[title]) valueNode.id = summaryFieldIds[title];
    card.append(element("strong", title), valueNode);
    summary.append(card);
  }
  const workspace = element("div", undefined, "review-workspace");
  const evidenceColumn = element("div", undefined, "evidence-column");
  const reviewRail = element("aside", undefined, "review-rail");
  reviewRail.setAttribute("aria-label", "Audit findings and review actions");
  workspace.append(evidenceColumn, reviewRail);
  host.append(summary);
  renderDispositionCard(host, data.disposition);
  host.append(workspace);
  const alreadyTriaged = data.reviews?.some((review) => review.action === "TRIAGE" && review.effective_decision === "NEEDS_REVIEW");
  if (audit && !audit.superseded && alreadyTriaged) {
    reviewRail.append(element("p", "This call has been triaged and remains in the review queue. Its evidence is still insufficient for a score.", "status"));
  } else if (audit && !audit.superseded) renderReviewForm(reviewRail, audit, data);
  else if (audit?.superseded) byId("call-status").textContent = "This audit uses superseded transcript evidence. Scoring actions are disabled; reload the current review queue.";

  const transcriptBlock = element("section", undefined, "block");
  transcriptBlock.append(element("h3", "Final redacted transcript"));
  const transcriptColumns = element("div", undefined, "transcript-columns");
  for (const label of ["Time", "Speaker", "Transcript"]) transcriptColumns.append(element("span", label));
  transcriptColumns.setAttribute("aria-hidden", "true");
  transcriptBlock.append(transcriptColumns);
  const transcriptList = element("div", undefined, "transcript-list");
  const utteranceIndex = new Map();
  for (const row of data.transcript || []) {
    const node = element("article", undefined, "transcript-row");
    const linkedFindings = (data.findings || []).filter((finding) => (finding.evidence_ids || []).includes(row.id));
    node.tabIndex = -1;
    node.setAttribute("aria-label", `${row.role}, ${timestamp(row.start_ms)} to ${timestamp(row.end_ms)}`);
    node.append(element("span", `${timestamp(row.start_ms)}–${timestamp(row.end_ms)}`, "transcript-time"));
    node.append(element("span", row.role, `speaker speaker-${String(row.role).toLowerCase()}`));
    node.append(element("p", row.text_redacted));
    if (linkedFindings.length) {
      node.classList.add("has-policy-evidence");
      node.append(element("span", `${linkedFindings.length} policy finding${linkedFindings.length === 1 ? "" : "s"} linked`, "policy-evidence-marker"));
    }
    utteranceIndex.set(row.id, { ...row, node });
    transcriptList.append(node);
  }
  if (!data.transcript?.length) transcriptList.append(element("p", "No final transcript is available for this call.", "empty-state"));
  transcriptBlock.append(transcriptList);
  evidenceColumn.append(transcriptBlock);

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
    reviewRail.append(dimensionBlock);
  }

  if (data.findings?.length) {
    const findingsBlock = element("section", undefined, "block");
    findingsBlock.append(element("h3", "Policy findings"));
    const rows = element("div", undefined, "finding-list");
    for (const finding of data.findings) {
      const card = element("article", undefined, "finding");
      card.append(element("strong", `${finding.rule_id} · ${finding.status} · ${finding.severity}`), element("p", finding.remediation));
      const links = element("div", undefined, "evidence-links");
      for (const utteranceId of finding.evidence_ids || []) addEvidenceButton(links, { utterance_id: utteranceId }, utteranceIndex);
      if (!links.childElementCount) links.append(element("span", "No transcript evidence attached."));
      card.append(links);
      rows.append(card);
    }
    findingsBlock.append(rows);
    reviewRail.append(findingsBlock);
  } else {
    const findingsBlock = element("section", undefined, "block");
    findingsBlock.append(element("h3", "Policy findings"), element("p", "No policy findings are available for this call.", "empty-state"));
    reviewRail.append(findingsBlock);
  }
  if (data.reviews?.length) {
    const reviewBlock = element("section", undefined, "block");
    reviewBlock.append(element("h3", "Human review history"));
    const rows = element("div", undefined, "review-list");
    for (const review of data.reviews) rows.append(element("article", `Review ${review.version} · ${review.action} · ${review.effective_decision} · ${review.reason}`, "review-entry"));
    reviewBlock.append(rows);
    reviewRail.append(reviewBlock);
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
  reviewRail.append(playback);
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

function setConnectionIndicator(text, isError = false) {
  const node = byId("connection-indicator");
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("error", isError);
}

// Apply an in-place refresh of only the fields the post-call state-refresh feed carries
// (processing_state, transcript_revision) for the call currently open in the detail view.
// This never triggers a full reload, so review-in-progress form state (draft reason text,
// score selections) is preserved. Events are de-duplicated by sequence so a redelivered or
// out-of-order event cannot re-apply a stale value after a newer one has already landed.
function applyCallUpdate(envelope) {
  if (!state.call || envelope.call_id !== state.callId) return;
  if (typeof envelope.sequence === "number" && envelope.sequence <= state.lastAppliedEventSequence) return;
  if (typeof envelope.sequence === "number") state.lastAppliedEventSequence = envelope.sequence;
  const payload = envelope.payload || {};
  if (typeof payload.processing_state === "string") {
    state.call.call.processing_state = payload.processing_state;
    const node = byId("summary-processing-state");
    if (node) node.textContent = payload.processing_state;
  }
  if (typeof payload.transcript_revision === "number") {
    state.call.call.transcript_revision = payload.transcript_revision;
    const node = byId("summary-transcript-revision");
    if (node) node.textContent = payload.transcript_revision;
  }
}

function handleEventFrame(frame) {
  let dataLine = null;
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue; // SSE comments and keepalives carry no data
    if (line.startsWith("data:")) dataLine = line.slice(5).replace(/^ /, "");
  }
  if (!dataLine) return;
  let envelope;
  try { envelope = JSON.parse(dataLine); } catch { return; }
  if (typeof envelope.sequence === "number") state.eventsCursor = envelope.sequence;
  if (envelope.type === "reset_required") {
    setConnectionIndicator("Feed reset, reloading…");
    if (state.callId) loadCall(state.callId);
    return;
  }
  if (envelope.type === "call.updated") applyCallUpdate(envelope);
}

function scheduleEventReconnect() {
  // A disconnected feed must never look like it is still current: show "Reconnecting…"
  // immediately, whether the connection failed outright or the stream simply ended (a
  // well-behaved server never voluntarily ends this feed while the client is attached).
  setConnectionIndicator("Reconnecting…", true);
  if (eventsReconnectTimer) return;
  eventsReconnectTimer = window.setTimeout(() => {
    eventsReconnectTimer = null;
    connectEventFeed();
  }, eventsRetryDelayMs);
  eventsRetryDelayMs = Math.min(eventsRetryDelayMs * 2, EVENTS_MAX_RETRY_MS);
}

async function connectEventFeed() {
  if (eventsAbortController) return;
  const controller = new AbortController();
  eventsAbortController = controller;
  const headers = new Headers({ Accept: "text/event-stream" });
  if (state.eventsCursor > 0) headers.set("Last-Event-ID", String(state.eventsCursor));
  let response;
  try {
    response = await fetch("/v1/events", { headers, credentials: "include", signal: controller.signal });
  } catch {
    eventsAbortController = null;
    if (!controller.signal.aborted) scheduleEventReconnect();
    return;
  }
  if (!response.ok || !response.body) {
    eventsAbortController = null;
    if (response.status === 401 || response.status === 403) {
      // Retrying would just repeat the same failure; stop and say so plainly.
      setConnectionIndicator("Disconnected", true);
      return;
    }
    scheduleEventReconnect();
    return;
  }
  setConnectionIndicator("Live");
  eventsRetryDelayMs = EVENTS_INITIAL_RETRY_MS;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        handleEventFrame(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch { /* fall through to the reconnect below, unless this was an intentional abort */ }
  eventsAbortController = null;
  if (!controller.signal.aborted) scheduleEventReconnect();
}

async function loadCall(callId) {
  state.callId = callId;
  state.audioGranted = false;
  state.lastAppliedEventSequence = 0;
  byId("call-status").classList.remove("error");
  byId("call-status").textContent = "Loading call evidence…";
  try {
    const detail = await request(`/v1/calls/${encodeURIComponent(callId)}`);
    if (detail.disposition) {
      try {
        const enriched = await request(`/v1/calls/${encodeURIComponent(callId)}/disposition`);
        if (enriched && enriched.status !== "PENDING") detail.disposition = { ...detail.disposition, ...enriched };
      } catch { /* keep the summary disposition fields already present on the call detail */ }
    }
    renderCall(detail);
    byId("call-status").textContent = detail.audit?.superseded
      ? "This audit uses superseded transcript evidence. Scoring actions are disabled; reload the current review queue."
      : "Current final redacted evidence loaded.";
    renderQueue();
    // The live state-refresh feed is scoped to the call open in the detail view; open it the
    // first time a call is opened and keep the same connection for later calls the reviewer opens.
    if (!eventsStarted) {
      eventsStarted = true;
      connectEventFeed();
    }
  } catch (error) {
    byId("call-status").textContent = error.message;
    byId("call-status").classList.add("error");
  }
}

byId("refresh-queue").addEventListener("click", loadQueue);
byId("refresh-live").addEventListener("click", loadLiveCalls);
const uploadForm = byId("call-upload-form");
const uploadStatus = byId("upload-status");
function uploadStatusText(message, error = false) {
  uploadStatus.textContent = message;
  uploadStatus.classList.toggle("error", error);
}
uploadForm.addEventListener("input", () => { state.uploadKey = null; });
uploadForm.addEventListener("change", () => { state.uploadKey = null; });
uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const audio = byId("upload-audio").files[0];
  if (!audio) {
    uploadStatusText("Choose a WAV or MP3 recording.", true);
    return;
  }
  if (!/\.(wav|mp3)$/i.test(audio.name)) {
    uploadStatusText("Choose a file with a .wav or .mp3 extension.", true);
    return;
  }
  if (audio.size > MAX_UPLOAD_BYTES) {
    uploadStatusText("The recording exceeds the 250 MiB upload limit.", true);
    return;
  }
  if (!state.uploadKey) state.uploadKey = crypto.randomUUID();
  const formData = new FormData(uploadForm);
  for (const control of uploadForm.elements) control.disabled = true;
  uploadStatusText("Uploading recording…");
  try {
    const result = await request("/v1/calls", {
      method: "POST",
      body: formData,
      headers: { "Idempotency-Key": state.uploadKey },
    });
    state.uploadKey = null;
    uploadForm.reset();
    uploadStatusText(`Call ${result.id} is queued for processing.`);
  } catch (error) {
    const conflictMessage = error.status !== 409 ? null
      : error.message === "External reference already exists"
        ? "That external reference is already in use. Choose a different reference."
        : "This upload key conflicts with a prior request. Retry the original unchanged upload, or edit the form to start a new upload.";
    uploadStatusText(conflictMessage || error.message, true);
  } finally {
    for (const control of uploadForm.elements) control.disabled = false;
  }
});
document.addEventListener("DOMContentLoaded", () => {
  loadQueue();
  loadLiveCalls();
  window.setInterval(() => { if (!document.hidden) loadLiveCalls(); }, 10_000);
});
