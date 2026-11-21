"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const H = {"X-Requested-With": "autotube", "Content-Type": "application/json"};
let timer = null;

function toast(msg, bad) { const t = $("#toast"); t.textContent = msg; t.style.borderColor = bad ? "var(--bad)" : "var(--line)"; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 5000); }
async function api(path, method = "GET", body) {
  const r = await fetch("/api" + path, {method, headers: H, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin"});
  let j = null; try { j = await r.json(); } catch (e) {}
  if (r.status === 401) { await askLogin(); return api(path, method, body); }
  if (!r.ok) throw new Error((j && (j.error || j.detail)) || r.statusText);
  return j;
}
async function act(label, fn, after) { try { const r = await fn(); toast(label + " ✓"); if (after) after(r); } catch (e) { toast(label + " failed: " + e.message, true); } }
async function askLogin() {
  const t = prompt("Dashboard token (DASHBOARD_TOKEN in your .env):"); if (!t) throw new Error("login required");
  await fetch("/api/login", {method: "POST", headers: H, body: JSON.stringify({token: t})});
}
const pill = (s) => { const c = {completed:"ok",published:"ok",scheduled:"ok",qc_passed:"ok",ok:"ok",failed:"bad",upload_failed:"bad",qc_failed:"bad",waiting_for_approval:"warn",awaiting_approval:"warn",running:"warn"}[s] || ""; return `<span class="pill ${c}">${esc(s)}</span>`; };
const n = (v, d = 0) => v == null ? "n/a" : Number(v).toLocaleString(undefined, {maximumFractionDigits: d});

const PAGES = {Overview, Topics, Pipeline, Videos, Analytics, Settings, Logs};
function nav() {
  const cur = (location.hash || "#Overview").slice(1);
  $("#nav").innerHTML = Object.keys(PAGES).map(p => `<a class="${p === cur ? "on" : ""}" href="#${p}">${p}</a>`).join("");
  return cur;
}
async function render() {
  clearTimeout(timer);
  const cur = nav(); const fn = PAGES[cur] || Overview;
  try { await fn(); } catch (e) { $("#view").innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`; }
  if (["Overview", "Pipeline"].includes(cur)) timer = setTimeout(render, 5000);
}
window.addEventListener("hashchange", render);

async function Overview() {
  const s = await api("/status");
  $("#chan").textContent = `${s.channel || ""} · ${s.publishing_mode}`;
  $("#banner").innerHTML = s.problems.map(p => `<div class="alert">⚠ ${esc(p)}</div>`).join("");
  const ai = Object.entries(s.ai).map(([k, v]) => `<div>${pill(v.ok ? "ok" : "failed")} ${esc(k)} <span class="muted small">${esc(v.detail || "")}</span></div>`).join("") || "none enabled";
  const vo = s.voices.map(v => `<div>${pill(v.available ? "ok" : "failed")} ${esc(v.name)}${v.placeholder ? " (placeholder – never publishable)" : ""}</div>`).join("");
  const r = s.resources, sc = s.scheduler, b = s.budget;
  $("#view").innerHTML = `<h2>Overview</h2>
  <div class="grid">
   <div class="card stat"><span class="muted">Automation</span><b>${sc.automation_enabled ? "ON" : "OFF"}</b>
     <div class="row"><button id="aon" ${sc.automation_enabled ? "disabled" : ""}>Start</button><button class="sec" id="aoff" ${sc.automation_enabled ? "" : "disabled"}>Stop</button></div></div>
   <div class="card stat"><span class="muted">Job queue</span><b>${sc.current ? esc(sc.current.name) : "idle"}</b><span class="small muted">${sc.queued} queued</span></div>
   <div class="card stat"><span class="muted">Needs approval</span><b>${s.counts.waiting_approvals}</b><a href="#Pipeline">review →</a></div>
   <div class="card stat"><span class="muted">Budget this month</span><b>₹${n(b.spent_this_month_inr, 2)} / ₹${n(b.monthly_limit_inr)}</b><span class="small muted">${b.paid_providers_allowed ? "paid allowed" : "free providers only"}</span></div>
  </div>
  <div class="grid">
   <div class="card"><h3>AI providers</h3>${ai}<div class="row"><button class="sec" id="chk">Re-check</button></div></div>
   <div class="card"><h3>Voice</h3>${vo}</div>
   <div class="card"><h3>This machine</h3><div class="small">RAM free: ${n(r.ram_free_gb, 1)} GB · Disk free: ${n(r.disk_free_gb, 1)} GB · CPUs: ${r.cpus ?? r.cpu_count ?? "?"}<br>GPU: ${r.gpu ? esc(JSON.stringify(r.gpu)) : "none detected"}<br>Suggested preset: <b>${esc(r.recommended_preset || "")}</b></div></div>
   <div class="card"><h3>YouTube</h3><div>${pill(s.youtube.authorized ? "ok" : "failed")} ${s.youtube.authorized ? "connected" : "not connected"}</div><a href="#Settings">manage →</a></div>
  </div>
  <div class="card"><h3>Quick actions</h3><div class="row">
   <button id="run">Run full pipeline now</button><button class="sec" id="smoke">Run smoke test (never uploads)</button>
   <button class="sec" id="disc">Discover topics</button></div>
   <div class="small muted">Recent jobs: ${sc.history.map(h => esc(h.name) + " → " + esc(h.outcome)).join(" · ") || "none"}</div></div>`;
  $("#aon").onclick = () => act("Automation started", () => api("/automation/start", "POST", {}), render);
  $("#aoff").onclick = () => act("Automation stopped", () => api("/automation/stop", "POST", {}), render);
  $("#chk").onclick = () => act("Provider check", () => api("/providers/check"), render);
  $("#run").onclick = () => act("Pipeline queued", () => api("/pipeline/run", "POST", {}), render);
  $("#smoke").onclick = () => act("Smoke test queued", () => api("/pipeline/run", "POST", {smoke: true}), render);
  $("#disc").onclick = () => act("Discovery queued", () => api("/topics/discover", "POST", {}), render);
}

async function Topics() {
  const ts = await api("/topics?limit=150");
  const rows = ts.map(t => `<tr><td>${t.id}</td><td>${esc(t.title)}<div class="small muted">${esc(t.source_name || "")} · ${esc(t.category || "")} · ${esc(t.signal_type || "")}</div></td>
   <td>${pill(t.status)}</td><td>${t.score_total ?? "–"}<div class="small muted">${esc(t.score_method || "")}</div></td>
   <td>${esc((t.selection_reason || t.rejection_reason || "").slice(0, 140))}</td>
   <td><button data-p="${t.id}">Produce</button> <button class="sec" data-r="${t.id}">Reject</button></td></tr>`).join("");
  $("#view").innerHTML = `<h2>Topics</h2><div class="card"><div class="row"><button id="disc">Discover (RSS + trends)</button><button class="sec" id="score">Score &amp; select best</button></div>
   <div class="row"><input id="nt" placeholder="Add your own topic title" style="flex:1"><button class="sec" id="add">Add</button></div></div>
   <div class="card"><table><tr><th>#</th><th>Topic</th><th>Status</th><th>Score</th><th>Reason</th><th></th></tr>${rows || '<tr><td colspan=6 class="muted">No topics yet – click Discover.</td></tr>'}</table></div>`;
  $("#disc").onclick = () => act("Discovery queued", () => api("/topics/discover", "POST", {}), () => setTimeout(render, 4000));
  $("#score").onclick = () => act("Scoring queued", () => api("/topics/score", "POST", {}), () => setTimeout(render, 4000));
  $("#add").onclick = () => act("Topic added", () => api("/topics/add", "POST", {title: $("#nt").value}), render);
  document.querySelectorAll("[data-p]").forEach(b => b.onclick = () => act("Pipeline queued for topic", () => api(`/topics/${b.dataset.p}/produce`, "POST", {}), () => location.hash = "#Pipeline"));
  document.querySelectorAll("[data-r]").forEach(b => b.onclick = () => act("Rejected", () => api(`/topics/${b.dataset.r}/reject`, "POST", {}), render));
}

async function Pipeline() {
  const [runs, appr] = await Promise.all([api("/runs"), api("/approvals")]);
  const ap = appr.map(a => `<div class="card"><b>${esc(a.stage)}</b> – run ${a.run_id}${a.video ? ` · video #${a.video.id} “${esc(a.video.title)}”` : ""}<div class="muted small">${esc(a.reason || "")}</div>
    <div class="row">${a.video ? `<a href="#Videos" data-v="${a.video.id}">open video</a>` : ""}<button class="ok" data-a="${a.run_id}|${a.stage}|1">Approve</button><button class="bad" data-a="${a.run_id}|${a.stage}|0">Reject</button></div></div>`).join("");
  const rr = runs.map(r => `<tr><td>${r.id}</td><td>${esc(r.name)}</td><td>${pill(r.status)}</td>
   <td class="small">${r.stages.map(s => `<span class="pill ${s.status === "completed" ? "ok" : s.status === "failed" ? "bad" : ""}" title="${esc(s.error || "")}">${esc(s.stage)}</span>`).join(" ")}</td>
   <td class="small">${esc((r.error || "").slice(0, 120))}</td>
   <td><button class="sec" data-pause="${r.id}">Pause</button> <button class="sec" data-resume="${r.id}">Resume</button></td></tr>`).join("");
  $("#view").innerHTML = `<h2>Pipeline</h2><h3>Waiting for you</h3>${ap || '<div class="card muted">Nothing is waiting for approval.</div>'}
   <h3>Runs</h3><div class="card"><table><tr><th>#</th><th>Type</th><th>Status</th><th>Stages</th><th>Error</th><th></th></tr>${rr || '<tr><td colspan=6 class="muted">No runs yet.</td></tr>'}</table></div>`;
  document.querySelectorAll("[data-a]").forEach(b => b.onclick = () => { const [r, st, ok] = b.dataset.a.split("|"); act(ok === "1" ? "Approved" : "Rejected", () => api(`/runs/${r}/approve`, "POST", {stage: st, approved: ok === "1"}), render); });
  document.querySelectorAll("[data-pause]").forEach(b => b.onclick = () => act("Paused", () => api(`/runs/${b.dataset.pause}/pause`, "POST", {}), render));
  document.querySelectorAll("[data-resume]").forEach(b => b.onclick = () => act("Resume queued", () => api(`/runs/${b.dataset.resume}/resume`, "POST", {}), render));
}

async function Videos() {
  const vs = await api("/videos");
  const rows = vs.map(v => `<tr><td>${v.id}</td><td>${esc(v.title)}${v.is_smoke ? ' <span class="pill warn">smoke</span>' : ""}</td><td>${pill(v.status)}</td>
   <td>${n(v.duration_seconds, 1)}s</td><td>${n(v.quality_score, 1)}</td><td>${v.upload ? pill(v.upload.status) + `<div class="small"><a target="_blank" rel="noopener" href="${esc(v.upload.url || "#")}">${esc(v.upload.url || "")}</a></div>` : "–"}</td>
   <td><button class="sec" data-o="${v.id}">Open</button></td></tr>`).join("");
  $("#view").innerHTML = `<h2>Videos</h2><div class="card"><table><tr><th>#</th><th>Title</th><th>Status</th><th>Length</th><th>QC</th><th>YouTube</th><th></th></tr>${rows || '<tr><td colspan=7 class="muted">No videos yet.</td></tr>'}</table></div><div id="vd"></div>`;
  document.querySelectorAll("[data-o]").forEach(b => b.onclick = () => openVideo(b.dataset.o));
}
async function openVideo(id) {
  const v = await api("/videos/" + id); const m = v.metadata_json || {}, q = v.qc_json || {}, sc = v.script || {};
  const checks = (q.checks || []).map(c => `<tr><td>${pill(c.passed ? "ok" : (c.critical ? "failed" : "warn"))}</td><td>${esc(c.name)}</td><td class="small">${esc(c.detail || "")}</td></tr>`).join("");
  const lic = (v.licenses || []).filter(Boolean).map(l => `<li>${esc(l.provider)} – ${esc(l.license_name)} – ${esc(l.author || "")}</li>`).join("");
  $("#vd").innerHTML = `<div class="card"><h2>#${v.id} ${esc(v.title)}</h2><div class="row" style="align-items:flex-start">
   ${v.final_path ? `<video controls src="/media/video/${v.id}"></video>` : ""}${v.thumbnail_path ? `<img class="th" src="/media/thumb/${v.id}">` : ""}
   <div style="flex:1;min-width:260px"><b>${esc(m.title || "")}</b><pre>${esc(m.description || "")}</pre><div class="small muted">Tags: ${esc((m.tags || []).join(", "))}</div></div></div>
   <div class="row"><button id="qc">Re-run QC</button>
    <select id="pv" style="width:auto"><option value="private">private</option><option value="unlisted">unlisted</option><option value="public">public (scheduled)</option></select>
    <button class="ok" id="up">Upload to YouTube</button></div>
   <h3>QC (score ${n(v.quality_score, 1)}/10 · ${q.passed ? "passed" : "not passed"})</h3><table>${checks}</table>
   <h3>Script</h3><pre>${esc(sc.narration || "")}</pre><h3>Asset licenses</h3><ul>${lic || "<li class=muted>none</li>"}</ul></div>`;
  $("#qc").onclick = () => act("QC", () => api(`/videos/${id}/qc`, "POST", {}), () => openVideo(id));
  $("#up").onclick = () => { const p = $("#pv").value; act("Upload queued", () => api(`/videos/${id}/upload`, "POST", {privacy: p, schedule: p === "public"})); };
  $("#vd").scrollIntoView();
}

async function Analytics() {
  const [a, mon, strat] = await Promise.all([api("/analytics"), api("/monetization").catch(() => null), api("/strategy")]);
  const rows = a.videos.map(x => `<tr><td>${esc(x.title || x.youtube_video_id)}</td><td>${n(x.views)}</td><td>${n(x.avg_view_percentage, 1)}%</td><td>${n(x.likes)}</td><td>${n(x.subs_gained)}</td><td>n/a</td><td>${x.revenue_status === "reported" ? n(x.revenue, 2) : "n/a"}</td><td class="small">${esc(x.snapshot_date)}</td></tr>`).join("");
  const ch = a.channel[0];
  const tiers = mon ? mon.tiers.map(t => `<div class="card"><b>${esc(t.name)}</b> ${t.met === null ? pill("unknown") : pill(t.met ? "ok" : "failed")}${Object.entries(t.requirements).map(([k, r]) => `<div class="small">${esc(k)}: ${n(r.have, 1)} / ${n(r.need)} ${r.note ? esc(r.note) : ""}</div>${r.have != null ? `<div class="bar"><i style="width:${Math.min(100, 100 * r.have / r.need)}%"></i></div>` : ""}`).join("")}</div>`).join("") + `<div class="small muted">${esc(mon.disclaimer)}</div>` : "<div class=muted>Unavailable.</div>";
  const wts = Object.entries(strat.weights).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ") || "all 1.0 (no data-driven changes yet)";
  $("#view").innerHTML = `<h2>Analytics</h2><div class="card"><div class="row"><button id="col">Collect analytics now</button><button class="sec" id="rep">Show weekly report</button><button class="sec" id="opt">Run optimizer</button></div>
   <div class="small muted">${a.notes.map(esc).join(" ")}</div>
   ${ch ? `<p>Subscribers: <b>${n(ch.subscribers)}</b> · Total views: <b>${n(ch.total_views)}</b> · Watch hours (365d): <b>${n(ch.watch_hours_365d, 1)}</b> <span class="muted small">(${esc(ch.snapshot_date)})</span></p>` : '<p class="muted">No channel stats yet. Connect YouTube and click Collect.</p>'}</div>
   <div class="card"><table><tr><th>Video</th><th>Views</th><th>Avg % viewed</th><th>Likes</th><th>Subs+</th><th>CTR</th><th>Revenue</th><th>As of</th></tr>${rows || '<tr><td colspan=8 class="muted">No analytics collected yet.</td></tr>'}</table></div>
   <h3>Optimizer – topic weights</h3><div class="card">${wts}<div class="row"><button class="sec" id="rst">Reset weights</button></div>
   ${strat.changes.slice(0, 6).map(c => `<div class="small muted">${esc(c.ts)} · ${esc(c.kind)} · ${esc(c.status)} · ${esc(c.reason)}</div>`).join("")}</div>
   <h3>Monetization progress</h3>${tiers}<pre id="out" style="display:none"></pre>`;
  $("#col").onclick = () => act("Collection queued", () => api("/analytics/collect", "POST", {}), () => setTimeout(render, 6000));
  $("#rep").onclick = () => act("Report", () => api("/reports/weekly"), r => { const o = $("#out"); o.style.display = "block"; o.textContent = r.markdown; });
  $("#opt").onclick = () => act("Optimizer", () => api("/optimizer/run", "POST", {}), r => { const o = $("#out"); o.style.display = "block"; o.textContent = (r.note || "") + "\n" + JSON.stringify(r, null, 2); });
  $("#rst").onclick = () => act("Weights reset", () => api("/strategy/reset", "POST", {}), render);
}

async function Settings() {
  const [c, yt] = await Promise.all([api("/config"), api("/youtube/status")]);
  const ch = c.channel || {}, sc = c.schedule || {};
  $("#view").innerHTML = `<h2>Settings</h2>
  <div class="card"><h3>Channel</h3><div class="grid">
   <div><label>Name</label><input id="c_name" value="${esc(ch.name)}"></div><div><label>Niche</label><input id="c_niche" value="${esc(ch.niche)}"></div>
   <div><label>Language</label><input id="c_lang" value="${esc(ch.language)}"></div><div><label>Timezone</label><input id="c_tz" value="${esc(ch.timezone)}"></div></div>
   <label>Style</label><input id="c_style" value="${esc(ch.style)}"><label>Target audience</label><input id="c_aud" value="${esc(ch.audience)}"></div>
  <div class="card"><h3>Schedule &amp; publishing</h3><div class="grid">
   <div><label>Publishing mode</label><select id="mode">${["MANUAL", "REVIEW", "PRIVATE", "AUTO"].map(m => `<option ${m === c.pipeline.publishing_mode ? "selected" : ""}>${m}</option>`).join("")}</select></div>
   <div><label>Production time (HH:MM)</label><input id="s_prod" value="${esc(sc.production_time)}"></div>
   <div><label>Publish time (HH:MM)</label><input id="s_pub" value="${esc(sc.publish_time)}"></div>
   <div><label>Videos per day</label><input id="s_n" type="number" min="1" max="5" value="${esc(sc.videos_per_day)}"></div></div>
   <div class="small muted">MANUAL/REVIEW: you approve every upload. PRIVATE: uploaded privately for you to publish in Studio. AUTO: scheduled publicly unless QC/research raised flags.</div>
   <div class="row"><button id="save">Save settings</button></div></div>
  <div class="card"><h3>YouTube</h3><div>${pill(yt.authorized ? "ok" : "failed")} ${yt.authorized ? "connected" : "not connected"} · client file: ${yt.client_secrets_configured ? "found" : "<b>missing</b> (set YOUTUBE_CLIENT_SECRETS in .env)"}</div>
   ${yt.needs_reauth_for && yt.needs_reauth_for.length ? `<div class="small" style="color:var(--warn)">Reconnect to grant: ${esc(yt.needs_reauth_for.join(", "))}</div>` : ""}
   ${yt.flow_error ? `<div class="small" style="color:var(--bad)">${esc(yt.flow_error)}</div>` : ""}
   <div class="row"><button id="yc">Connect YouTube</button><button class="sec" id="yd">Disconnect</button><button class="sec" id="yr">Refresh upload statuses</button></div></div>
  <div class="card"><h3>Test providers</h3><div class="row"><button class="sec" id="pc">Check AI + voice now</button></div><pre id="pcout" style="display:none"></pre>
   <div class="small muted">API keys (Pexels, Pixabay, …) are never entered here – put them in .env.</div></div>`;
  $("#save").onclick = () => act("Settings saved", () => api("/config", "POST", {
    channel: {name: $("#c_name").value, niche: $("#c_niche").value, language: $("#c_lang").value, timezone: $("#c_tz").value, style: $("#c_style").value, audience: $("#c_aud").value},
    pipeline: {publishing_mode: $("#mode").value},
    schedule: {production_time: $("#s_prod").value, publish_time: $("#s_pub").value, videos_per_day: +$("#s_n").value}}), render);
  $("#yc").onclick = () => act("Consent window opened on the AutoTube computer", () => api("/youtube/connect", "POST", {}));
  $("#yd").onclick = () => act("Disconnected", () => api("/youtube/disconnect", "POST", {}), render);
  $("#yr").onclick = () => act("Refreshed", () => api("/youtube/refresh", "POST", {}));
  $("#pc").onclick = () => act("Checked", () => api("/providers/check"), r => { const o = $("#pcout"); o.style.display = "block"; o.textContent = JSON.stringify(r, null, 2); });
}

async function Logs() {
  const [lg, nt] = await Promise.all([api("/logs?limit=200"), api("/notifications")]);
  $("#view").innerHTML = `<h2>Logs &amp; notifications</h2><div class="card"><div class="row"><button class="sec" id="nr">Mark notifications read</button></div>
   <table>${nt.map(x => `<tr><td class="small">${esc(x.ts)}</td><td>${pill(x.level)}</td><td><b>${esc(x.title)}</b> ${esc(x.message)}</td></tr>`).join("") || '<tr><td class="muted">None</td></tr>'}</table></div>
   <div class="card"><table>${lg.map(x => `<tr><td class="small">${esc(x.ts)}</td><td>${esc(x.level)}</td><td class="small">${esc(x.logger)}</td><td class="small">${esc(x.message)}</td></tr>`).join("")}</table></div>`;
  $("#nr").onclick = () => act("Marked read", () => api("/notifications/read", "POST", {}), render);
}
render();
