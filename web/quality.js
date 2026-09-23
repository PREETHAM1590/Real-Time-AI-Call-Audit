(() => {
  const status = document.querySelector("#report-status");
  const ownSection = document.querySelector("#own-report");
  const teamSection = document.querySelector("#team-report");
  const ownItems = document.querySelector("#own-items");
  const teamItems = document.querySelector("#team-items");
  const startInput = document.querySelector("#period-start");
  const endInput = document.querySelector("#period-end");

  function element(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }

  async function getJson(path) {
    const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!response.ok) throw Object.assign(new Error(`Request failed (${response.status})`), { status: response.status });
    return response.json();
  }

  function renderOwn(rows) {
    ownItems.replaceChildren();
    if (!rows.length) ownItems.append(element("p", "No reviewed calls are available for this period."));
    for (const row of rows) {
      const card = element("article", undefined, "report-card");
      card.append(element("h3", `Call ${row.call_id}`));
      const meta = element("p", `${new Date(row.created_at).toLocaleString()} · ${row.processing_state} · ${row.team_id}`);
      card.append(meta);
      const scores = document.createElement("dl");
      for (const [label, value] of [
        ["Machine score", row.machine_score], ["Machine decision", row.machine_decision],
        ["Reviewed score", row.reviewed_score], ["Reviewed decision", row.reviewed_decision],
        ["Rubric version", row.rubric_version], ["Model artifact", row.model_artifact],
      ]) {
        scores.append(element("dt", label), element("dd", value ?? "Not available"));
      }
      card.append(scores);
      if (row.checklist?.length) {
        card.append(element("h4", "Checklist"));
        const list = document.createElement("ul");
        for (const dimension of row.checklist) {
          list.append(element("li", `${dimension.id}: ${dimension.status}; machine ${dimension.machine_score ?? "—"}; reviewed ${dimension.reviewed_score ?? "—"}. ${dimension.reason ?? ""}`));
        }
        card.append(list);
      }
      if (row.coaching_notes?.length) {
        card.append(element("h4", "Saved coaching notes"));
        const list = document.createElement("ul");
        for (const note of row.coaching_notes) list.append(element("li", `${note.kind}: ${note.text}`));
        card.append(list);
      }
      ownItems.append(card);
    }
    ownSection.hidden = false;
  }

  function renderTeam(report) {
    teamItems.replaceChildren();
    if (!report.cohorts?.length) {
      teamItems.append(element("p", "No calls were found for this reporting period."));
      return;
    }
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const header = document.createElement("tr");
    for (const label of ["Team", "Rubric", "Model", "Agents", "Calls", "Machine avg", "Reviewed avg", "Suppression"]) header.append(element("th", label));
    head.append(header);
    const body = document.createElement("tbody");
    for (const cohort of report.cohorts) {
      const row = document.createElement("tr");
      const values = cohort.suppressed
        ? [cohort.team_id, cohort.rubric_version, cohort.model_artifact, "Suppressed", "Suppressed", "Suppressed", "Suppressed", "Fewer than five agents"]
        : [cohort.team_id, cohort.rubric_version, cohort.model_artifact, cohort.distinct_agents, cohort.sample_count, cohort.machine_average ?? "—", cohort.reviewed_average ?? "—", ""];
      for (const value of values) row.append(element("td", value));
      body.append(row);
    }
    table.append(head, body);
    teamItems.append(table);
  }

  async function loadTeam() {
    const start = startInput.value;
    const end = endInput.value;
    if (!start || !end) return;
    status.textContent = "Loading team report…";
    try {
      const endExclusive = new Date(`${end}T00:00:00Z`);
      endExclusive.setUTCDate(endExclusive.getUTCDate() + 1);
      const params = new URLSearchParams({ start: `${start}T00:00:00Z`, end: endExclusive.toISOString() });
      renderTeam(await getJson(`/v1/reports/team?${params}`));
      teamSection.hidden = false;
      status.textContent = "Team report loaded.";
    } catch (error) {
      status.textContent = error.status === 403 ? "Your account does not have team report access." : "The team report could not be loaded.";
      status.classList.add("error");
    }
  }

  const today = new Date();
  const prior = new Date(today.getTime() - 29 * 86400000);
  const isoDate = (date) => date.toISOString().slice(0, 10);
  startInput.value = isoDate(prior);
  endInput.value = isoDate(today);
  document.querySelector("#period-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadTeam();
  });

  (async () => {
    try {
      const response = await getJson("/v1/me/scores");
      if (!response || !Array.isArray(response.items)) throw new Error("Malformed score report");
      const rows = response.items;
      renderOwn(rows);
      status.textContent = "Your reviewed scores are loaded.";
    } catch (error) {
      if (error.status !== 403) {
        status.textContent = "Your report could not be loaded.";
        status.classList.add("error");
        return;
      }
      ownSection.hidden = true;
      await loadTeam();
    }
  })();
})();
