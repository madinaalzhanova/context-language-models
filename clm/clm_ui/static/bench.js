// The Benchmark view: run the same ContextBench tasks under several arms (CLM, the base
// model without CLM, other models or skills), then compare them with standard errors and
// follow every running episode at once. It polls /api/benchmark every 2 s.
// Loaded after app.js and evolution.js and uses their helpers ($, h, fmt, pct, ...).
"use strict";

const BENCH = { name: null, timer: 0, sel: null, preset: [], snap: null };
const ARM_COLORS = 4;   // --arm-1 .. --arm-4 in style.css, assigned in arm order
const armVar = (i) => `var(--arm-${(i % ARM_COLORS) + 1})`;

async function openBenchmark() {
  $("bench").showModal();
  await benchCatalogs();
  await benchRuns();
}

async function benchCatalogs() {
  try { if (!TASKS.length) TASKS = (await (await fetch("/api/tasks")).json()).tasks || []; } catch {}
  const byFamily = {};
  for (const x of TASKS) (byFamily[x.family || "other"] ||= []).push(x);
  $("bench-tasks").replaceChildren(...Object.entries(byFamily).map(([fam, xs]) =>
    h("optgroup", { label: fam }, xs.map((x) => h("option", { value: x.name }, `${x.name} · ${x.ops ?? "?"} ops`)))));
  try { if (!SKILLS.length) SKILLS = (await (await fetch("/api/skills")).json()).skills || []; } catch {}
  const f = $("bench-form").elements;
  try {
    const d = await (await fetch("/api/defaults")).json();
    if (!f.model.value) f.model.value = d.config.model;
    BENCH.sandbox = d.sandbox;
  } catch {}
  try {
    if (!MODELS.length) {
      MODELS = (await (await fetch("/api/models")).json()).models || [];
      $("model-list").replaceChildren(...MODELS.map((x) => h("option", { value: x.id }, x.name)));
    }
  } catch {}
  if (!$("bench-arms").querySelector("tr.arm")) benchSetArms(BENCH.preset.length ? BENCH.preset : null);
  benchEstimate();
}

// ------------------------------------------------------------------ the arms editor
function benchArmRow(a = {}) {
  const sel = (name, opts, v) => h("select", { "data-k": name }, opts.map(([val, label]) =>
    h("option", { value: val, text: label, selected: val === v })));
  const steer = h("select", { "data-k": "steering" }, h("option", { value: "", text: "none" }),
    SKILLS.map((x) => h("option", { value: x.id, text: x.label, selected: x.id === a.steering })));
  const row = h("tr", { class: "arm" },
    h("td", {}, h("i", { class: "sw-arm" })),
    h("td", {}, h("input", { "data-k": "label", value: a.label || "", placeholder: "label", required: true })),
    h("td", {}, sel("agent", [["clm", "CLM"], ["baseline", "base model, no CLM"]], a.agent || "clm")),
    h("td", {}, h("input", { "data-k": "model", value: a.model || "", list: "model-list", placeholder: "default model", autocomplete: "off" })),
    h("td", {}, sel("method_skill", [["clm", "clm"], ["base", "base"], ["none", "none"]], a.method_skill || "clm")),
    h("td", {}, steer),
    h("td", {}, h("button", { type: "button", class: "ghost small", title: "Remove this arm",
      onclick: () => { row.remove(); benchArmColors(); benchEstimate(); } }, "×")));
  // A baseline with CLM's method text would read instructions for a context file it lacks.
  row.querySelector("[data-k=agent]").addEventListener("change", (e) => {
    const m = row.querySelector("[data-k=method_skill]");
    if (e.target.value === "baseline" && m.value === "clm") m.value = "base";
    if (e.target.value === "clm" && m.value === "base") m.value = "clm";
  });
  return row;
}

function benchSetArms(arms) {
  arms = arms || [{ label: "clm", agent: "clm", method_skill: "clm" },
                  { label: "base", agent: "baseline", method_skill: "base" }];
  $("bench-arms").replaceChildren(
    h("tr", {}, ["", "label", "agent", "model", "method text", "steering skill", ""].map((t) => h("th", { text: t }))),
    ...arms.map(benchArmRow));
  benchArmColors();
}
function benchArmColors() {
  $("bench-arms").querySelectorAll("tr.arm").forEach((tr, i) => (tr.querySelector(".sw-arm").style.background = armVar(i)));
}
function benchArms() {
  return [...$("bench-arms").querySelectorAll("tr.arm")].map((tr) => {
    const a = {};
    tr.querySelectorAll("[data-k]").forEach((el) => (a[el.dataset.k] = el.value.trim()));
    return a;
  });
}
$("bench-add").addEventListener("click", () => {
  $("bench-arms").append(benchArmRow({ label: `arm${benchArms().length + 1}` }));
  benchArmColors(); benchEstimate();
});
$("bench-preset").addEventListener("click", () => { benchSetArms(BENCH.preset.length ? BENCH.preset : null); benchEstimate(); });

const benchPicked = () => [...$("bench-tasks").selectedOptions].map((o) => o.value);
function benchEstimate() {
  const f = $("bench-form").elements, n = (k) => Math.max(1, parseInt(f[k].value || "1", 10));
  const tasks = benchPicked().length, arms = benchArms().length;
  const pairs = Math.max(1, Math.floor(n("workers") / Math.max(1, arms)));
  $("bench-estimate").textContent = tasks && arms
    ? `${arms} arms × ${tasks} tasks × ${n("reps")} repeats = ${fmt(arms * tasks * n("reps"))} task runs ` +
      `on ${BENCH.sandbox || "the sandbox"}, ` +
      (f.paired.checked ? `${pairs} pair${pairs > 1 ? "s" : ""} of ${arms} runs at a time` : `${n("workers")} at a time`) +
      `. Graded runs are kept when you resume.`
    : "Pick tasks (Ctrl+click for several) and at least one arm.";
}
$("bench-tasks").addEventListener("change", benchEstimate);
for (const k of ["reps", "workers", "paired"]) $("bench-form").elements[k].addEventListener("input", benchEstimate);

$("bench-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = $("bench-form").elements;
  const body = { name: f.name.value, model: f.model.value, reps: f.reps.value, workers: f.workers.value,
                 paired: f.paired.checked, tasks: benchPicked(), arms: benchArms() };
  const r = await fetch("/api/benchmark/start", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) { $("bench-estimate").textContent = d.error || "could not start"; return; }
  await benchRuns(d.name);
});

// ------------------------------------------------------------------ runs
async function benchRuns(select) {
  let d = { runs: [], current: null };
  try { d = await (await fetch("/api/benchmarks")).json(); } catch {}
  BENCH.preset = d.preset || BENCH.preset;
  const cur = d.current;
  $("bench-run").replaceChildren(h("option", { value: "", text: "+ New benchmark" }),
    ...d.runs.map((r) => h("option", { value: r.name },
      `${r.name} · ${r.arms.join(" vs ")} · ${r.n_done}/${r.n_runs}${cur && cur.name === r.name ? " · " + cur.status : ""}`)));
  const want = select ?? (cur && ["running", "starting", "stopping"].includes(cur.status) ? cur.name : BENCH.name);
  $("bench-run").value = want && d.runs.some((r) => r.name === want) ? want : "";
  if (select && $("bench-run").value !== select) {
    $("bench-run").append(h("option", { value: select, text: `${select} · starting` }));
    $("bench-run").value = select;
  }
  benchSelect($("bench-run").value);
}
$("bench-run").addEventListener("change", (e) => benchSelect(e.target.value));

function benchSelect(name) {
  BENCH.name = name || null;
  BENCH.sel = null;
  clearTimeout(BENCH.timer);
  $("bench-form").hidden = !!name;
  $("bench-view").hidden = !name;
  $("bench-stop").hidden = true;
  if (!name) { benchStatus("idle"); return; }
  $("bench-pane").replaceChildren(h("p", { class: "hint", text: "Click a running tile or a dot under All runs." }));
  $("bench-pane-title").textContent = "Select a run";
  benchPoll();
}

function benchStatus(st) {
  const el = $("bench-status");
  el.dataset.state = { running: "running", starting: "running", stopping: "running",
    done: "waiting_user", error: "error" }[st] || "idle";
  el.textContent = st;
}

async function benchPoll() {
  const name = BENCH.name;
  if (!name || !$("bench").open) return;
  let s = null;
  try { s = await (await fetch(`/api/benchmark?name=${encodeURIComponent(name)}`)).json(); } catch {}
  if (name !== BENCH.name) return;
  if (s && s.name) { BENCH.snap = s; benchRender(s); }
  if (BENCH.sel) await benchDetail(BENCH.sel, true);
  if (!s || ["running", "starting", "stopping"].includes(s.status)) BENCH.timer = setTimeout(benchPoll, 2000);
}

// ------------------------------------------------------------------ rendering
const pm = (x, f, fse = f) => (x == null || x.mean == null ? "–" : f(x.mean) + (x.se != null ? ` ± ${fse(x.se)}` : ""));
const pctN = (v) => `${(100 * v).toFixed(1)}%`;
const tok = (v) => fmt(Math.round(v));
const one = (v) => v.toFixed(1);
const secs = (v) => (v >= 90 ? `${(v / 60).toFixed(1)} min` : `${v.toFixed(0)} s`);
const signed = (f) => (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + f(Math.abs(v));

function benchRender(s) {
  const c = s.config || {};
  benchStatus(s.status);
  $("bench-stop").hidden = !["running", "starting"].includes(s.status);
  const running = s.cells.filter((x) => x.state === "running").length;
  $("bench-summary").replaceChildren(...[
    h("div", {}, h("b", { text: `${fmt(s.n_done)} of ${fmt(s.n_runs)} runs done` }),
      running ? ` · ${running} running` : "",
      ` · ${(c.tasks || []).length} tasks × ${c.reps} repeats · ${c.workers} in parallel` +
      (c.paired === false ? "" : " · arms in pairs")),
    s.error ? h("div", { class: "notice bad", text: s.error }) : null,
  ].filter(Boolean));

  const color = Object.fromEntries(s.arms.map((a, i) => [a.label, armVar(i)]));
  const sgAny = s.arms.some((a) => a.stats.single_guess.n);
  const maxScore = Math.max(1e-9, ...s.arms.map((a) => a.stats.score.mean ?? 0));
  const costFmt = (a) => (v) => (a.stats.cost_metric === "usd" ? `$${v.toFixed(4)}` : tok(v));

  // Overall: one row per arm.
  $("bench-results").replaceChildren(
    h("tr", {}, ["arm", "score", sgAny ? "single_guess" : null, "Δ score vs " + (s.arms[0]?.label ?? "ref"),
      "peak tokens", "edits", "cost / run", "ops reached", "time / run", "graded"].filter((t) => t != null).map((t) => h("th", { text: t }))),
    ...s.arms.map((a, i) => {
      const st = a.stats, d = a.vs_ref?.score;
      return h("tr", {},
        h("td", { class: "arm-cell" }, h("div", { class: "mono" },
          h("i", { class: "sw-arm", style: `background:${color[a.label]}` }), a.label),
          h("div", { class: "hint", text: a.about })),
        h("td", { class: "num" }, h("div", { class: "bar" }, h("i", { style: `width:${100 * (st.score.mean ?? 0) / maxScore}%;background:${color[a.label]}` })),
          pm(st.score, pctN)),
        sgAny ? h("td", { class: "num", text: pm(st.single_guess, pctN) }) : null,
        h("td", { class: "num", text: i === 0 ? "reference" : d ? pm(d, signed(pctN), pctN) : "–",
          title: "difference of means; ± is the standard error of the difference (independent runs)" }),
        h("td", { class: "num", text: pm(st.peak_tokens, tok) }),
        h("td", { class: "num", text: pm(st.edits, one) }),
        h("td", { class: "num", text: pm(st.cost, costFmt(a)) }),
        h("td", { class: "num", text: pm(st.ops_frac, pctN), title: "ops delivered before the run ended (budget wall or finish)" }),
        h("td", { class: "num", text: pm(st.duration_s, secs) }),
        h("td", { class: "num", text: `${st.n_graded}/${st.n_runs}` }));
    }));

  // Per task: score of each arm.
  $("bench-bytask").replaceChildren(
    h("tr", {}, h("th", { text: "task" }), ...s.arms.map((a) => h("th", {},
      h("i", { class: "sw-arm", style: `background:${color[a.label]}` }), a.label))),
    ...(c.tasks || []).map((t) => {
      const best = Math.max(...s.arms.map((a) => a.tasks[t]?.score.mean ?? -1));
      return h("tr", {}, h("td", { class: "mono", text: t }), ...s.arms.map((a) => {
        const x = a.tasks[t]?.score;
        return h("td", { class: "num" + (x?.mean != null && x.mean === best && s.arms.length > 1 ? " best" : ""),
          text: pm(x, pctN) + (x?.n ? ` (n=${x.n})` : "") });
      }));
    }));

  // Running now: one tile per live episode.
  const live = s.cells.filter((x) => x.state === "running");
  $("bench-live-hint").textContent = live.length ? "context tokens per turn; dashed line = the limit" : "";
  $("bench-tiles").replaceChildren(...(live.length ? live.map((x) => benchTile(x, color[x.arm])) :
    [h("p", { class: "hint", text: s.status === "running" ? "starting the next runs…" : "Nothing is running." })]));

  // All runs: tasks × arms, one dot per repeat.
  const reps = c.reps || 1;
  const byId = Object.fromEntries(s.cells.map((x) => [x.id, x]));
  $("bench-matrix").replaceChildren(
    h("tr", {}, h("th", { text: "task" }), ...s.arms.map((a) => h("th", { text: a.label }))),
    ...(c.tasks || []).map((t) => h("tr", {}, h("td", { class: "mono", text: t }), ...s.arms.map((a) => h("td", {},
      ...Array.from({ length: reps }, (_, r) => {
        const x = byId[`${a.label}/${t}__r${r}`] || { state: "queued" };
        return h("button", { type: "button", class: `dot ${x.state}${BENCH.sel === x.id ? " sel" : ""}`,
          style: x.state === "done" ? `--c:${color[a.label]}` : null,
          title: `${a.label} · ${t} · repeat ${r} · ${x.state}` + (x.score != null ? ` · score ${pctN(x.score)}` : "") +
            (x.error ? ` · ${x.error}` : ""),
          onclick: () => x.id && benchDetail(x.id) }, x.score != null ? Math.round(100 * x.score) : "");
      }))))));

  const log = $("bench-log"), stick = atBottom(log);
  log.textContent = s.log || "";
  if (stick) log.scrollTop = log.scrollHeight;
}

function benchTile(x, color) {
  const L = x.live || {}, ops = L.ops || {};
  const frac = ops.total ? ops.delivered / ops.total : 0;
  return h("div", { class: "tile" + (BENCH.sel === x.id ? " sel" : ""), onclick: () => benchDetail(x.id) },
    h("div", { class: "tile-head" }, h("i", { class: "sw-arm", style: `background:${color}` }),
      h("b", { text: x.arm }), h("span", { class: "mono hint", text: `${x.task} · r${x.rep}` }),
      h("span", { class: "tile-state", text: L.state || "starting" })),
    spark(L.series || [], L.limit, color, 260, 56),
    h("div", { class: "tile-prog" }, h("i", { style: `width:${100 * frac}%` })),
    h("div", { class: "tile-nums" },
      h("span", {}, `ops ${ops.delivered ?? "–"}/${ops.total ?? "–"}`),
      h("span", {}, `ctx ${fmt(L.tokens)}${L.limit ? " / " + fmt(L.limit) : ""}`),
      h("span", {}, `calls ${L.calls ?? 0}`), h("span", {}, `edits ${L.edits ?? 0}`)),
    h("div", { class: "tile-cmd mono", text: L.last_command ? "$ " + firstLine(L.last_command) : "" }));
}

// Context tokens per turn as a line, with the limit dashed; hover reads a turn off it.
function spark(series, limit, color, w, hgt) {
  const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${hgt}`);
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  const top = Math.max(limit || 0, ...series, 1), pad = 3;
  const X = (i) => pad + (series.length > 1 ? (i / (series.length - 1)) * (w - 2 * pad) : 0);
  const Y = (v) => hgt - pad - (v / top) * (hgt - 2 * pad);
  const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); svg.append(e); return e; };
  el("line", { x1: 0, x2: w, y1: hgt - pad, y2: hgt - pad, class: "spark-base" });
  if (limit) el("line", { x1: 0, x2: w, y1: Y(limit), y2: Y(limit), class: "spark-limit" });
  if (series.length) {
    el("polyline", { points: series.map((v, i) => `${X(i)},${Y(v)}`).join(" "), fill: "none",
      stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
    el("circle", { cx: X(series.length - 1), cy: Y(series[series.length - 1]), r: 3, fill: color });
  }
  svg.setAttribute("aria-label", series.length ? `context tokens per turn, latest ${fmt(series[series.length - 1])}` : "no turns yet");
  const dot = el("circle", { r: 4, fill: color, class: "spark-hover", visibility: "hidden" });
  const tip = el("text", { class: "spark-tip", x: 4, y: 11 });
  svg.addEventListener("mousemove", (e) => {
    if (!series.length) return;
    const r = svg.getBoundingClientRect(), i = Math.round(((e.clientX - r.left) / r.width * w - pad) / (w - 2 * pad) * (series.length - 1));
    const k = Math.max(0, Math.min(series.length - 1, i));
    dot.setAttribute("cx", X(k)); dot.setAttribute("cy", Y(series[k])); dot.setAttribute("visibility", "visible");
    tip.textContent = `point ${k + 1}: ${fmt(series[k])} tokens`;
  });
  svg.addEventListener("mouseleave", () => { dot.setAttribute("visibility", "hidden"); tip.textContent = ""; });
  return svg;
}

// ------------------------------------------------------------------ one run
async function benchDetail(id, refresh) {
  BENCH.sel = id;
  let d;
  try { d = await (await fetch(`/api/benchmark/run?name=${encodeURIComponent(BENCH.name)}&id=${encodeURIComponent(id)}`)).json(); } catch { return; }
  if (d.error || BENCH.sel !== id) return;
  const s = BENCH.snap, i = s ? s.arms.findIndex((a) => a.label === id.split("/")[0]) : 0;
  const color = armVar(Math.max(0, i)), L = d.live || {}, r = d.result;
  $("bench-pane-title").textContent = id;
  const pane = $("bench-pane"), feedEl = pane.querySelector(".bench-feed");
  const stick = !feedEl || atBottom(feedEl);
  const feed = h("div", { class: "bench-feed" }, ...(d.feed.length ? d.feed.map((ev) =>
    h("div", { class: `fe fe-${ev.kind}` }, h("span", { class: "fe-kind", text: ev.kind }),
      h("span", { class: ev.kind === "command" ? "mono" : "", text: ev.text }))) :
    [h("p", { class: "hint", text: "No events yet: the sandbox is starting." })]));
  pane.replaceChildren(...[
    h("div", { class: "hint mono", text: d.run_dir }),
    spark(L.series || [], L.limit, color, 420, 110),
    h("div", { class: "tile-nums" },
      h("span", {}, `state ${L.state}`), h("span", {}, `ops ${L.ops?.delivered ?? "–"}/${L.ops?.total ?? "–"}`),
      h("span", {}, `ctx ${fmt(L.tokens)}`), h("span", {}, `calls ${L.calls}`),
      h("span", {}, `commands ${L.cmds}`), h("span", {}, `edits ${L.edits}`)),
    r ? h("div", { class: "notice" + (r.error ? " bad" : "") }, r.error ? r.error :
      `score ${pctN(r.score)}` + (r.single_guess != null ? ` · single_guess ${pctN(r.single_guess)}` : "") +
      ` · peak ${fmt(r.peak_tokens)} tokens · ${r.edits ?? 0} edits · ended by ${r.terminated_by || "–"}` +
      (r.cost != null ? ` · ${r.cost_metric === "usd" ? "$" + r.cost.toFixed(4) : fmt(Math.round(r.cost)) + " tokens"}` : "")) : null,
    h("h4", { text: "Recent events" }), feed].filter(Boolean));
  if (stick || !refresh) feed.scrollTop = feed.scrollHeight;
  if (!refresh) document.querySelectorAll("#bench .tile, #bench .dot").forEach((e) => e.classList.remove("sel"));
}

// Right-click the benchmark picker: delete the benchmark it shows.
$("bench-run").title = "Right-click to delete this benchmark";
$("bench-run").addEventListener("contextmenu", (e) => {
  const name = $("bench-run").value;
  if (!name) return;
  e.preventDefault();
  benchMenuClose();
  const menu = h("div", { class: "ctx-menu", id: "bench-menu", style: `left:${e.clientX}px;top:${e.clientY}px` },
    h("button", { type: "button", class: "danger", onclick: () => { benchMenuClose(); benchDelete(name); } },
      `Delete benchmark ${name}…`));
  $("bench").append(menu);
  setTimeout(() => document.addEventListener("click", benchMenuClose, { once: true }));
});
function benchMenuClose() { $("bench-menu")?.remove(); }
document.addEventListener("keydown", (e) => e.key === "Escape" && benchMenuClose());

async function benchDelete(name) {
  if (!confirm(`Delete benchmark "${name}"?

Its results, logs and every run's folder are removed from disk. This cannot be undone.`)) return;
  const r = await fetch("/api/benchmark/delete", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  const d = await r.json();
  if (!r.ok) { alert(d.error || "could not delete"); return; }
  clearTimeout(BENCH.timer);
  BENCH.name = null;
  await benchRuns("");
}

$("bench-stop").addEventListener("click", async () => { await fetch("/api/benchmark/stop", { method: "POST" }); benchPoll(); });
$("bench-close").addEventListener("click", () => { clearTimeout(BENCH.timer); $("bench").close(); });
$("btn-bench").addEventListener("click", openBenchmark);

// http://127.0.0.1:7860/#benchmark opens this view; #benchmark/<name> opens a benchmark,
// #benchmark/<name>/<arm>/<task>__r<k> one of its runs.
if (location.hash.startsWith("#benchmark")) {
  const [, name, ...run] = location.hash.split("/").map(decodeURIComponent);
  $("bench").showModal();
  benchCatalogs().then(() => benchRuns(name || undefined)).then(() => run.length && benchDetail(run.join("/")));
}
