// The Evolution view: start a clm_icl run, then follow it. It reads the run's files
// through /api/evolution (state.json, rounds/, episodes/, evolution.log) every 2 s.
// Loaded after app.js and uses its helpers ($, h, fmt, splitLines, atBottom, ...).
"use strict";

const EVO = { name: null, timer: 0, sid: null };

async function openEvolution() {
  $("evo").showModal();
  await evoCatalogs();
  await evoRuns();
}

async function evoCatalogs() {
  try { if (!TASKS.length) TASKS = (await (await fetch("/api/tasks")).json()).tasks || []; } catch {}
  const opts = () => TASKS.map((x) => h("option", { value: x.name }, `${x.name} · ${x.ops ?? "?"} ops`));
  $("evo-train").replaceChildren(...opts());
  $("evo-dev").replaceChildren(...opts());
  try {
    const k = (await (await fetch("/api/skills")).json()).skills || [];
    $("evo-init").replaceChildren(h("option", { value: "", text: "none: start with no skill" }),
      ...k.map((x) => h("option", { value: x.id, text: x.label })));
  } catch {}
  const f = $("evo-form").elements;
  try {
    const d = await (await fetch("/api/defaults")).json();
    if (!f.model.value) f.model.value = d.config.model;
    EVO.sandbox = d.sandbox;
  } catch {}
  try {
    if (!MODELS.length) {
      MODELS = (await (await fetch("/api/models")).json()).models || [];
      $("model-list").replaceChildren(...MODELS.map((x) => h("option", { value: x.id }, x.name)));
    }
  } catch {}
  evoEstimate();
}

const evoPicked = (id) => [...$(id).selectedOptions].map((o) => o.value);

function evoEstimate() {
  const f = $("evo-form").elements, n = (k) => Math.max(1, parseInt(f[k].value || "1", 10));
  const train = evoPicked("evo-train").length, dev = evoPicked("evo-dev").length || train;
  const max = n("reps") * (dev + n("rounds") * (train + n("candidates") * dev));
  $("evo-estimate").textContent = train
    ? `Up to ${fmt(max)} task runs (each one ContextBench task on ${EVO.sandbox || "the sandbox"}), ` +
      `${n("workers")} at a time, plus one proposer call per round. Runs already graded are reused when you resume.`
    : "Pick training tasks (Ctrl+click for several). A few short tasks of one family is a good start.";
}
for (const id of ["evo-train", "evo-dev"]) $(id).addEventListener("change", evoEstimate);
$("evo-proposer").addEventListener("change", (e) => {
  const teacher = e.target.value === "teacher";
  $("evo-teacher").hidden = !teacher;
  $("evo-form").elements.proposer_model.required = teacher;
});
for (const k of ["rounds", "candidates", "reps", "workers"]) $("evo-form").elements[k].addEventListener("input", evoEstimate);

async function evoRuns(select) {
  let d = { runs: [], current: null };
  try { d = await (await fetch("/api/evolutions")).json(); } catch {}
  const cur = d.current;
  $("evo-run").replaceChildren(h("option", { value: "", text: "+ New evolution" }),
    ...d.runs.map((r) => h("option", { value: r.name },
      `${r.name} · round ${r.round}${cur && cur.name === r.name ? " · " + cur.status : ""}`)));
  const want = select ?? (cur && ["running", "starting", "stopping"].includes(cur.status) ? cur.name : EVO.name);
  $("evo-run").value = want && d.runs.some((r) => r.name === want) ? want : "";
  // A run that just started has no state.json yet: show it anyway.
  if (select && $("evo-run").value !== select) {
    $("evo-run").append(h("option", { value: select, text: `${select} · starting` }));
    $("evo-run").value = select;
  }
  evoSelect($("evo-run").value);
}
$("evo-run").addEventListener("change", (e) => evoSelect(e.target.value));

function evoSelect(name) {
  EVO.name = name || null;
  EVO.sid = null;
  clearTimeout(EVO.timer);
  $("evo-form").hidden = !!name;
  $("evo-view").hidden = !name;
  $("evo-stop").hidden = true;
  if (!name) { evoStatus("idle"); return; }
  $("evo-pane").replaceChildren();
  $("evo-pane-title").textContent = "Select a skill or a round";
  $("evo-use").hidden = true;
  evoPoll();
}

function evoStatus(st) {
  const el = $("evo-status");
  el.dataset.state = { running: "running", starting: "running", stopping: "running",
    done: "waiting_user", error: "error" }[st] || "idle";
  el.textContent = st;
}

async function evoPoll() {
  const name = EVO.name;
  if (!name || !$("evo").open) return;
  let s = null;
  try { s = await (await fetch(`/api/evolution?name=${encodeURIComponent(name)}`)).json(); } catch {}
  if (name !== EVO.name) return;
  if (s && s.name) evoRender(s);
  if (!s || ["running", "starting", "stopping"].includes(s.status)) EVO.timer = setTimeout(evoPoll, 2000);
}

const pct = (x) => (x == null ? "–" : `${(100 * x).toFixed(1)}%`);
const costText = (c, m) => (c == null ? "–" : m === "usd" ? `$${c.toFixed(4)}` : `${fmt(Math.round(c))} ${m || ""}`);

function evoRender(s) {
  const c = s.config || {};
  evoStatus(s.status);
  $("evo-stop").hidden = !["running", "starting"].includes(s.status);
  const done = Object.values(s.episodes || {}).reduce((a, e) => a + e.done, 0);
  $("evo-summary").replaceChildren(...[
    h("div", {}, h("b", { text: `Round ${s.round} of ${c.rounds ?? "?"}` }),
      ` · incumbent ${s.incumbent || "–"} · task runs ${fmt(done)} of up to ${fmt(s.n_episodes_max)}`),
    h("div", { class: "hint" }, `agent ${c.model} · ` +
      (c.proposer === "teacher" ? `teacher ${c.proposer_model}` : "self-evolution") + ` · optimize ${c.optimize} · ` +
      `${(c.train_tasks || []).length} training, ${(c.dev_tasks || []).length || "same"} development tasks · ` +
      `${c.candidates} candidates × ${c.reps} repeat(s)`),
    s.dev_equals_train ? h("div", { class: "notice" }, "No development tasks: skills are selected on the " +
      "training tasks the proposer has read, so the gate is optimistic. Check the best skill on other tasks.") : null,
    s.error ? h("div", { class: "notice bad", text: s.error }) : null,
  ].filter(Boolean));

  const pill = { incumbent: "ok", passed: "info", rejected: "bad", retired: "", candidate: "warn" };
  $("evo-skills").replaceChildren(
    h("tr", {}, ["skill", "round", "status", "dev score", "graded", "cost / run", "gate"].map((t) => h("th", { text: t }))),
    ...(s.skills || []).map((k) => h("tr", { class: k.id === EVO.sid ? "sel" : "", onclick: () => evoSkill(k.id) },
      h("td", { class: "mono", text: k.id }), h("td", { text: k.round ?? "" }),
      h("td", {}, h("span", { class: `pill ${pill[k.status] ?? ""}`, text: k.status || "" })),
      h("td", { text: pct(k.acc) }), h("td", { text: k.n ? `${k.n_valid}/${k.n}` : "–" }),
      h("td", { text: costText(k.cost, k.cost_metric) }),
      h("td", { class: "hint", text: k.gate || "" }))));

  $("evo-rounds").replaceChildren(...(s.rounds || []).map((r) => {
    const g = r.gate;
    return h("div", { class: "evo-round" }, h("b", { text: `Round ${r.round}: ` }),
      g ? (g.winner ? `new incumbent ${g.winner}` : `kept ${g.incumbent_before}`) +
        ` (${(g.candidates || []).length} candidates, ${(g.rejected_at_validation || []).length} invalid)` : "in progress",
      r.has_note ? h("button", { class: "ghost small", onclick: () => evoRound(r.round, "note.md") }, "runs compared") : null,
      r.has_analysis ? h("button", { class: "ghost small", onclick: () => evoRound(r.round, "analysis.md") }, "proposer's analysis") : null);
  }));
  const log = $("evo-log"), stick = atBottom(log);
  log.textContent = s.log || "";
  if (stick) log.scrollTop = log.scrollHeight;
}

async function evoSkill(sid) {
  EVO.sid = sid;
  const d = await (await fetch(`/api/evolution/skill?name=${encodeURIComponent(EVO.name)}&sid=${encodeURIComponent(sid)}`)).json();
  if (d.error) return;
  $("evo-pane-title").textContent = d.parent ? `${sid} (changes from ${d.parent})` : sid;
  $("evo-use").hidden = !d.text;
  $("evo-pane").replaceChildren(...diffRows(d.text || "(no skill: zero-shot)", d.diff));
  document.querySelectorAll("#evo-skills tr").forEach((tr) =>
    tr.classList.toggle("sel", tr.firstChild?.textContent === sid));
}

async function evoRound(k, file) {
  EVO.sid = null;
  $("evo-use").hidden = true;
  const d = await (await fetch(`/api/evolution/round?name=${encodeURIComponent(EVO.name)}&k=${k}&file=${file}`)).json();
  $("evo-pane-title").textContent = `Round ${k}: ` +
    (file === "note.md" ? "the runs the proposer compared" : "the proposer's analysis");
  $("evo-pane").replaceChildren(...splitLines(d.text || "").map((l) => h("div", { class: "ln", text: l })));
}

// A text with a line diff (agent.line_diff): added lines green, removed lines red.
function diffRows(text, diff) {
  const lines = splitLines(text);
  if (!diff) return lines.map((l) => h("div", { class: "ln", text: l }));
  const rows = [];
  let j = 0;
  for (const op of diff) {
    if (op.op === "eq") { for (let k = 0; k < op.n; k++) rows.push(h("div", { class: "ln", text: lines[j++] ?? "" })); }
    else if (op.op === "add") { for (const l of op.lines) rows.push(h("div", { class: "ln add", text: lines[j++] ?? l })); }
    else { for (const l of op.lines) rows.push(h("div", { class: "ln del", text: l })); }
  }
  return rows;
}

$("evo-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = $("evo-form").elements, body = {};
  for (const el of f) if (el.name && el.type !== "select-multiple") body[el.name] = el.value;
  body.train_tasks = evoPicked("evo-train");
  body.dev_tasks = evoPicked("evo-dev");
  const r = await fetch("/api/evolution/start", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) { $("evo-estimate").textContent = d.error || "could not start"; return; }
  await evoRuns(d.name);
});

$("evo-stop").addEventListener("click", async () => {
  await fetch("/api/evolution/stop", { method: "POST" });
  evoPoll();
});
$("evo-close").addEventListener("click", () => { clearTimeout(EVO.timer); $("evo").close(); });
$("btn-evo").addEventListener("click", openEvolution);
$("evo-use").addEventListener("click", () => {
  // The skill library names an evolved skill evo:<run>/<id>.
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    saved.steering = `evo:${EVO.name}/${EVO.sid}`;
    localStorage.setItem(LS_KEY, JSON.stringify(saved));
  } catch {}
  $("evo").close();
  openDialog();
});

// http://127.0.0.1:7860/#evolution opens this view; #evolution/<name> opens a run.
if (location.hash.startsWith("#evolution")) {
  const name = decodeURIComponent(location.hash.split("/")[1] || "");
  $("evo").showModal();
  evoCatalogs().then(() => evoRuns(name || undefined));
}
