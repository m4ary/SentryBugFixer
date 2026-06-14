const $ = (sel) => document.querySelector(sel);
const api = async (url, opts) => {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
};
const esc = (s) => (s ?? "").toString().replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtUsd = (n) => "$" + Number(n || 0).toFixed(4);
const fmtTok = (n) => Number(n || 0).toLocaleString();
const fmtTime = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString() : "—");
const statusLabel = (s) => (s || "").replace(/_/g, " ");

let activeProject = null;

async function loadProjects() {
  const projects = await api("/api/projects");
  const el = $("#projects");
  if (!projects.length) {
    el.innerHTML = '<div class="empty">No projects yet. Click “+ Add project”.</div>';
    return;
  }
  el.innerHTML = projects
    .map(
      (p) => `
      <div class="proj-item" data-pid="${p.id}">
        <div>
          <strong>${esc(p.name)}</strong>
          <div class="meta">${esc(p.gitlab_url)} · ${esc(p.sentry_url)} · branch <code>${esc(p.default_branch)}</code></div>
        </div>
        <div class="actions">
          <button data-issues="${p.id}">View issues</button>
          <button class="ghost" data-edit="${p.id}">Edit</button>
          <button class="ghost" data-del="${p.id}">Delete</button>
        </div>
      </div>`
    )
    .join("");

  el.querySelectorAll("[data-issues]").forEach((b) =>
    b.addEventListener("click", () => loadIssues(projects.find((p) => p.id == b.dataset.issues)))
  );
  el.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Delete this project?")) return;
      await api(`/api/projects/${b.dataset.del}`, { method: "DELETE" });
      loadProjects();
    })
  );
  el.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => openProjectModal(projects.find((p) => p.id == b.dataset.edit)))
  );
}

// --- Add / Edit project modal ---
let editingProjectId = null;

function openProjectModal(project) {
  editingProjectId = project ? project.id : null;
  $("#project-modal-title").textContent = project ? "Edit project" : "Add project";
  $("#project-save").textContent = project ? "Save changes" : "Add project";
  const f = $("#project-form");
  f.name.value = project ? project.name : "";
  f.gitlab_url.value = project ? project.gitlab_url : "";
  f.sentry_url.value = project ? project.sentry_url : "";
  f.default_branch.value = project ? project.default_branch : "main";
  $("#project-modal").hidden = false;
  setTimeout(() => f.name.focus(), 0);
}

function closeProjectModal() {
  $("#project-modal").hidden = true;
  editingProjectId = null;
}

$("#add-project-btn").addEventListener("click", () => openProjectModal(null));
$("#project-modal-close").addEventListener("click", closeProjectModal);
$("#project-cancel").addEventListener("click", closeProjectModal);
$("#project-modal").addEventListener("click", (e) => { if (e.target.id === "project-modal") closeProjectModal(); });
$("#project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = {
    name: f.name.value,
    gitlab_url: f.gitlab_url.value,
    sentry_url: f.sentry_url.value,
    default_branch: f.default_branch.value || "main",
  };
  try {
    const url = editingProjectId ? `/api/projects/${editingProjectId}` : "/api/projects";
    const saved = await api(url, {
      method: editingProjectId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (activeProject && editingProjectId && activeProject.id == editingProjectId) activeProject = saved;
    closeProjectModal();
    loadProjects();
  } catch (err) {
    alert("Could not save project: " + err.message);
  }
});

async function loadIssues(project) {
  activeProject = project;
  $("#issues-card").hidden = false;
  $("#issues-project").textContent = project.name;
  const box = $("#issues");
  box.innerHTML = "Loading…";
  try {
    const issues = await api(`/api/projects/${project.id}/issues`);
    if (!issues.length) {
      box.innerHTML = '<div class="empty">No unresolved issues 🎉</div>';
      return;
    }
    box.innerHTML = `<table>
      <thead><tr><th>Issue</th><th>Level</th><th>Events</th><th></th></tr></thead>
      <tbody>${issues.map((i) => `<tr>
            <td><strong>${esc(i.shortId || i.id)}</strong> — ${esc(i.title)}<br><span class="muted">${esc(i.culprit)}</span></td>
            <td><span class="badge ${esc(i.level) || "info"}">${esc(i.level || "—")}</span></td>
            <td>${esc(i.count)}</td>
            <td>${actionCell(i)} <button class="ghost" data-hist="${esc(i.id)}" data-title="${esc(i.title || i.id)}">History</button></td>
          </tr>`).join("")}</tbody></table>`;

    box.querySelectorAll("[data-fix]").forEach((b) =>
      b.addEventListener("click", () => openStart(project.id, b.dataset.fix, b.dataset.title, false))
    );
    box.querySelectorAll("[data-resume]").forEach((b) =>
      b.addEventListener("click", () => openStart(project.id, b.dataset.resume, b.dataset.title, true))
    );
    box.querySelectorAll("[data-viewjob]").forEach((b) =>
      b.addEventListener("click", () => openLog(b.dataset.viewjob, b.dataset.title))
    );
    box.querySelectorAll("[data-hist]").forEach((b) =>
      b.addEventListener("click", () => openHistory(project.id, b.dataset.hist, b.dataset.title))
    );
  } catch (e) {
    box.innerHTML = `<div class="empty">Could not load issues: ${esc(e.message)}</div>`;
  }
}

async function loadJobs() {
  const jobs = await api("/api/jobs");
  const el = $("#jobs");
  if (!jobs.length) {
    el.innerHTML = '<div class="empty">No fix jobs yet.</div>';
    return;
  }
  el.innerHTML = `<table>
    <thead><tr><th>Issue</th><th>Status</th><th>Tokens (in/out)</th><th>Cost</th><th>Result</th><th>Log</th></tr></thead>
    <tbody>${jobs
      .map(
        (j) => `<tr>
          <td>${esc(j.issue_title || j.issue_id)}</td>
          <td><span class="status ${esc(j.status)}">${esc(statusLabel(j.status))}</span></td>
          <td>${fmtTok(j.input_tokens)} / ${fmtTok(j.output_tokens)}</td>
          <td>${fmtUsd(j.cost_usd)}</td>
          <td>${j.mr_url ? `<a href="${esc(j.mr_url)}" target="_blank">Merge request ↗</a>` : "—"}</td>
          <td><button class="ghost" data-log="${esc(j.id)}" data-title="${esc(j.issue_title || j.issue_id)}">📜 View log</button></td>
        </tr>`
      )
      .join("")}</tbody></table>`;

  el.querySelectorAll("[data-log]").forEach((b) =>
    b.addEventListener("click", () => openLog(b.dataset.log, b.dataset.title))
  );
}

function actionCell(i) {
  const title = esc(i.title || i.id);
  if (i.action === "review")
    return `<span class="badge info">Under review</span> ${i.mr_url ? `<a href="${esc(i.mr_url)}" target="_blank">MR ↗</a>` : ""}`;
  if (i.action === "running")
    return `<button class="ghost" data-viewjob="${esc(i.job_id)}" data-title="${title}">▶ Running… view</button>`;
  if (i.action === "resume")
    return `<button data-resume="${esc(i.id)}" data-title="${title}">⟳ Resume</button>`;
  return `<button data-fix="${esc(i.id)}" data-title="${title}">🔧 Fix it</button>`;
}

// --- Start-fix modal (optional user instructions) ---
let pendingFix = null;

function openStart(projectId, issueId, title, resume) {
  pendingFix = { projectId, issueId, title };
  $("#start-title").textContent = (resume ? "Resume fix — " : "Fix issue — ") + (title || issueId);
  $("#start-sub").textContent = resume
    ? "Resuming the previous attempt (reuses prior work). Add any extra guidance, then Start:"
    : "Add optional instructions for the agent, then Start:";
  $("#start-input").value = "";
  $("#start-modal").hidden = false;
  setTimeout(() => $("#start-input").focus(), 0);
}

function closeStart() {
  $("#start-modal").hidden = true;
  pendingFix = null;
}

$("#start-close").addEventListener("click", closeStart);
$("#start-cancel").addEventListener("click", closeStart);
$("#start-modal").addEventListener("click", (e) => { if (e.target.id === "start-modal") closeStart(); });
$("#start-go").addEventListener("click", async () => {
  if (!pendingFix) return;
  const { projectId, issueId, title } = pendingFix;
  const instructions = $("#start-input").value.trim();
  closeStart();
  try {
    const job = await api(`/api/projects/${projectId}/fix`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ issue_id: issueId, instructions }),
    });
    loadJobs();
    if (activeProject) loadIssues(activeProject);
    openLog(job.id, title || issueId);
  } catch (e) {
    alert("Failed to start fix: " + e.message);
  }
});

// --- Live log popup (WebSocket) ---
let logWS = null;

function openLog(jobId, title) {
  $("#log-modal").hidden = false;
  $("#log-title").textContent = title ? `Fix log — ${title}` : `Job ${jobId}`;
  $("#log-body").textContent = "";
  $("#log-status").textContent = "";
  $("#log-status").className = "status";
  $("#log-mr").hidden = true;
  $("#log-conn").textContent = "connecting…";

  if (logWS) { try { logWS.close(); } catch (_) {} }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/jobs/${jobId}`);
  logWS = ws;
  const body = $("#log-body");

  ws.onopen = () => ($("#log-conn").textContent = "live");
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "log") {
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      body.textContent += m.line + "\n";
      if (atBottom) body.scrollTop = body.scrollHeight;
    } else if (m.type === "status") {
      const s = $("#log-status");
      s.textContent = m.status + (m.cost_usd != null ? ` · ${fmtUsd(m.cost_usd)}` : "");
      s.className = "status " + m.status;
      if (m.mr_url) { const a = $("#log-mr"); a.href = m.mr_url; a.hidden = false; }
      if (["success", "error", "no_changes"].includes(m.status)) {
        loadJobs();
        if (activeProject) loadIssues(activeProject); // refresh button states (e.g. → Under review / Resume)
      }
    } else if (m.type === "error") {
      body.textContent += "[" + m.message + "]\n";
    }
  };
  ws.onclose = () => { if (logWS === ws) $("#log-conn").textContent = "disconnected"; };
  ws.onerror = () => ($("#log-conn").textContent = "connection error");
}

function closeLog() {
  $("#log-modal").hidden = true;
  if (logWS) { try { logWS.close(); } catch (_) {} logWS = null; }
}

$("#log-close").addEventListener("click", closeLog);
$("#log-modal").addEventListener("click", (e) => { if (e.target.id === "log-modal") closeLog(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeLog(); closeStart(); closeProjectModal(); }
});

// --- Issue history modal ---
async function openHistory(projectId, issueId, title) {
  $("#hist-title").textContent = `History — ${title || issueId}`;
  $("#hist-body").innerHTML = "Loading…";
  $("#hist-modal").hidden = false;
  try {
    const data = await api(`/api/projects/${projectId}/issues/${encodeURIComponent(issueId)}/jobs`);
    if (!data.jobs.length) {
      $("#hist-body").innerHTML = '<div class="empty">No fix attempts yet for this issue.</div>';
      return;
    }
    $("#hist-body").innerHTML = `
      <p class="muted">${data.jobs.length} attempt(s) · total ${fmtTok(data.total_input_tokens)} in / ${fmtTok(
        data.total_output_tokens
      )} out · total cost <strong>${fmtUsd(data.total_cost_usd)}</strong></p>
      <table>
        <thead><tr><th>When</th><th>Status</th><th>Tokens (in/out)</th><th>Cost</th><th>Result</th><th></th></tr></thead>
        <tbody>${data.jobs
          .map(
            (j) => `<tr>
            <td>${fmtTime(j.created_at)}</td>
            <td><span class="status ${esc(j.status)}">${esc(statusLabel(j.status))}</span></td>
            <td>${fmtTok(j.input_tokens)} / ${fmtTok(j.output_tokens)}</td>
            <td>${fmtUsd(j.cost_usd)}</td>
            <td>${j.mr_url ? `<a href="${esc(j.mr_url)}" target="_blank">MR ↗</a>` : "—"}</td>
            <td><button class="ghost" data-histlog="${esc(j.id)}" data-title="${esc(j.issue_title || j.issue_id)}">log</button></td>
          </tr>`
          )
          .join("")}</tbody></table>`;
    $("#hist-body").querySelectorAll("[data-histlog]").forEach((b) =>
      b.addEventListener("click", () => { $("#hist-modal").hidden = true; openLog(b.dataset.histlog, b.dataset.title); })
    );
  } catch (e) {
    $("#hist-body").innerHTML = `<div class="empty">Could not load history: ${esc(e.message)}</div>`;
  }
}
$("#hist-close").addEventListener("click", () => ($("#hist-modal").hidden = true));
$("#hist-modal").addEventListener("click", (e) => { if (e.target.id === "hist-modal") $("#hist-modal").hidden = true; });

// --- Models & pricing ---
async function loadModels() {
  const data = await api("/api/models");
  $("#models-current").textContent = data.current ? `· using ${data.current}` : "";
  $("#models").innerHTML = `<table>
    <thead><tr><th>Model</th><th>Input $/1M</th><th>Output $/1M</th></tr></thead>
    <tbody>${data.models
      .map(
        (m) => `<tr class="${m.id === data.current ? "model-current" : ""}">
          <td>${esc(m.id)}${m.id === data.current ? " ✓" : ""}</td>
          <td>$${m.input_per_mtok.toFixed(2)}</td>
          <td>$${m.output_per_mtok.toFixed(2)}</td>
        </tr>`
      )
      .join("")}</tbody></table>`;
}

// Poll jobs so running fixes update live.
setInterval(loadJobs, 4000);
loadProjects();
loadJobs();
loadModels();
