// CLM Inspector page. Renders the session's event stream (see clm_ui/agent.py for the
// events) into three columns: chat, the turn-by-turn timeline, and the file system.
"use strict";

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString());
const kb = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const firstLine = (s) => (s || "").split("\n")[0].slice(0, 90);
const HEADER_RE = /^\[\[CTX_TURN (\d+) role=(\w+)\]\]$/;
// Same line breaks as Python's str.splitlines, which the server's diff uses.
const LINE_BREAKS = new RegExp("\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]");
const splitLines = (s) => {
  const out = (s || "").split(LINE_BREAKS);
  if (out.length && out[out.length - 1] === "") out.pop();
  return out;
};

// ------------------------------------------------------------------ state
let S;
function fresh() {
  return {
    session: null, mode: "chat",
    turn: 0, card: null, lastCall: 0,
    ctx: { path: null, text: "", diff: null, by: null, live: false, summary: "", written: false },
    ctxPreview: null,
    prefix: null, ctxTurns: [], protect: 2, budget: null,
    counters: { calls: 0, cmds: 0, applied: 0, nudges: 0 },
    roots: [], mtimes: new Map(), selected: null, lastCmd: "",
  };
}

// ------------------------------------------------------------------ socket
let ws;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (m) => handle(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connect, 1000);
}
const send = (obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));

// ------------------------------------------------------------------ events
const H = {
  reset() { resetUI(); },
  no_session() { resetUI(); openDialog(); },

  session(ev) {
    S.session = ev; S.mode = ev.config.mode; S.ctx.path = ev.ctx_file;
    const c = ev.config;
    $("session-info").textContent = (c.mode === "task"
      ? `ContextBench task ${ev.task} · ${ev.model} · paper settings`
      : `chat · ${ev.model} · budget ${fmt(c.context_budget_tokens)} · gate ${c.allow_edit_growth}`) +
      ` · ${ev.sandbox} sandbox · ${ev.dir}`;
    $("banner").hidden = !ev.warning; $("banner").textContent = ev.warning || "";
    $("btn-stop").disabled = false;
    $("chat-hint").textContent = c.mode === "task"
      ? "the task's instruction and ops, as the stream delivers them" : "what the user sees";
    select({ where: "sandbox", path: ev.ctx_file });
    updateComposer();
  },

  status(ev) {
    const st = ev.state, el = $("status");
    el.dataset.state = st;
    el.textContent = {
      starting: "starting", waiting_user: "waiting for you", grading: "grading",
      thinking: "model thinking", running: "running command", finished: `finished: ${ev.detail || ""}`,
    }[st] || st;
    if (S.work && st !== "finished") tickWork();
    else {
      const w = $("working");
      w.hidden = st !== "starting" && st !== "grading";
      w.textContent = ev.detail || st;
    }
    if (st === "finished") { $("btn-stop").disabled = true; }
    updateComposer(st);
  },

  prefix(ev) { S.prefix = ev; if (isCtxSelected()) renderViewer(); },

  user_message(ev) {
    if (ev.kind === "task") {
      chat("user task", ev.text, "Task instruction (pinned: not in the context file)");
      return;
    }
    if (ev.kind === "op") {
      // An op, or ContextBench's note when a marker did not advance or a submit came early.
      const isOp = /^=== .* op\(s\) remaining after this\) ===/.test(ev.text);
      chat("user task", ev.text, isOp ? `Op ${ev.delivered} of ${ev.total} (from the task's stream)` : "ContextBench");
      if (isOp) { S.work = { t0: ev.t, calls: 0, cmds: 0 }; tickWork(); }
      return;
    }
    chat("user", ev.text, "You");
    if (ev.queued) chat("system", "Queued: the agent is still working and will read it when it asks for the next message.");
  },

  user_turn(ev) {
    S.work = { t0: ev.t, calls: 0, cmds: 0, edits: 0 };
    tickWork();
    timeline(h("div", { class: "event" },
      `User message entered the context as a user turn (message ${ev.index}): `, firstLine(ev.text)));
  },

  budget(ev) {
    S.budget = ev;
    S.turn += 1;
    const card = newCard(`Turn ${S.turn}`);
    if (ev.notices.length) {
      S.counters.nudges += ev.action === "continue" ? 1 : 0;
      for (const n of ev.notices) {
        card.body.append(h("div", { class: "notice" + (ev.action === "final_turn" ? " bad" : "") },
          h("b", { text: ev.action === "final_turn" ? "Over the limit: final turn. " : "Harness nudge. " }), n));
      }
      card.el.classList.add("has-ctx");
    }
    gauge(ev.tokens, ev.limit, ev.budget, ev.nudge_ratios);
    counters();
  },

  rollback(ev) {
    const c = S.card || newCard("Rollback");
    c.body.append(h("div", { class: "notice bad" }, h("b", { text: "Rollback. " }),
      `Context crossed the limit; the harness dropped the newest ${ev.dropped} turn(s) to ${fmt(ev.tokens_after)} tokens ` +
      `and pinned a ledger note (retry ${ev.retry}/${ev.max_retry}).`));
    c.el.classList.add("has-ctx");
    counters();
  },

  context(ev) {
    S.ctxTurns = ev.turns; S.protect = ev.protect;
    if (ev.rendered != null) S.ctxPreview = ev.rendered;
    ctxmap(ev.turns);
    if (S.budget) gauge(ev.tokens, ev.limit, S.budget.budget, S.budget.nudge_ratios);
    if (S.card) S.card.meta.textContent = `sent ${fmt(ev.tokens)} tok · ${ev.turns.length} msgs · snapshot turn-${String(ev.snapshot).padStart(4, "0")}`;
    if (isCtxSelected()) renderViewer();
  },

  ctx_preview(ev) {
    if (ev.rendered != null) S.ctxPreview = ev.rendered;
    if (isCtxSelected()) renderViewer();
  },

  llm_request(ev) {
    S.lastCall = ev.call; S.counters.calls += 1; counters();
    if (S.work) { S.work.calls += 1; tickWork(); }
  },

  llm_response(ev) {
    const c = S.card || newCard(`Turn ${++S.turn}`);
    c.head.append(h("span", { class: "pill info", text: `LM call #${ev.call} · ${ev.latency_s}s` }));
    if (ev.reasoning) {
      c.body.append(h("div", { class: "sec reasoning" },
        h("div", { class: "sec-title" }, "Reasoning (hidden thinking)"),
        h("div", { class: "scroll" }, h("pre", { text: ev.reasoning }))));
    }
    if (ev.content) {
      c.body.append(h("div", { class: "sec" }, h("div", { class: "sec-title" }, "Model text (THOUGHT)"),
        h("div", { class: "scroll" }, h("pre", { text: ev.content }))));
    }
    if (ev.tool) {
      S.lastCmd = ev.command;
      c.tool = h("div", { class: "sec" },
        h("div", { class: "sec-title" }, `Tool call: ${ev.tool}`, c.ctxBadge = h("span")),
        h("pre", { class: "cmd", text: ev.command }));
      c.body.append(c.tool);
    } else if (S.mode === "chat" && ev.content.trim()) {
      c.body.append(h("div", { class: "notice info" }, "No tool call: in chat mode this is the final answer to the user."));
    } else {
      c.body.append(h("div", { class: "notice" }, "No tool call. The harness asks the model to issue a bash command, and the turn counts as a step."));
    }
  },

  mirror_write(ev) {
    S.ctx.written = true;
    setCtx(ev.text, ev.diff, "harness", `Rewritten by the harness before the command: ${ev.turns} editable turns.`);
  },

  command_start(ev) {
    S.lastCmd = ev.command; S.counters.cmds += 1; counters();
    if (S.work) { S.work.cmds += 1; tickWork(); }
  },

  mirror_live(ev) {
    setCtx(ev.text, ev.diff, "model", "The model's command is editing this file now.", true);
  },

  command_result(ev) {
    const c = S.card; if (!c) return;
    const out = [ev.stdout, ev.stderr].filter(Boolean).join("\n") || "(no output)";
    const long = out.split("\n").length > 14;
    const body = h("pre", { class: "out", text: out });
    c.body.append(h("div", { class: "sec" },
      h("div", { class: "sec-title" }, "Output",
        h("span", { class: "pill " + (ev.return_code === 0 ? "ok" : "bad"), text: `exit ${ev.return_code}` }),
        h("span", { class: "pill", text: `${ev.exec_s}s` })),
      long ? h("details", {}, h("summary", { text: `${out.split("\n").length} lines` }), body) : body));
    c.lastOut = ev;
  },

  ctx_edit(ev) {
    const c = S.card;
    const free = ev.applied && c && c.lastOut && !c.lastOut.stdout.trim() && !c.lastOut.stderr.trim() && c.lastOut.return_code === 0;
    if (ev.status !== "none" || ev.touched) {
      const st = {
        applied: ["ok", "edit applied"], applied_over: ["warn", "applied, still over the limit"],
        grew: ["warn", "applied, context grew"], rejected: ["bad", "edit rejected by the gate"],
        no_match: ["", "no change: edit matched nothing"],
      }[ev.status] || ["", ev.status];
      if (ev.status === "applied" || ev.status === "applied_over" || ev.status === "grew") S.counters.applied += 1;
      if (c) {
        if (c.ctxBadge) c.ctxBadge.replaceWith(h("span", { class: "pill info", text: "edits the context file" }));
        const sec = h("div", { class: "sec ctx-sec" },
          h("div", { class: "sec-title" }, "Context file",
            h("span", { class: "pill " + st[0], text: st[1] }),
            free ? h("span", { class: "pill ok", text: "free turn: no task step used" }) : null),
          ev.before != null ? h("div", { class: "delta" },
            `${fmt(ev.before)} → ${fmt(ev.after)} tokens (${ev.after - ev.before >= 0 ? "+" : ""}${fmt(ev.after - ev.before)})`) : null,
          miniDiff(ev.diff),
          h("details", {}, h("summary", { text: "harness receipt" }), h("pre", { class: "out", text: ev.note })));
        c.body.append(sec);
        c.el.classList.add("has-ctx");
      }
      const verb = ev.applied ? "applied" : ev.status === "rejected" ? "rejected (the harness restores the file on the next turn)" : "made no change";
      setCtx(ev.text, ev.diff, "model", `Model edit at turn ${S.turn} ${verb}` +
        (ev.before != null ? `: ${fmt(ev.before)} → ${fmt(ev.after)} tokens.` : "."));
    } else if (S.ctx.live || ev.text != null) {
      // Unchanged file. The server keeps the file's text only on the newest such event,
      // so after a replay this may be the only copy of it.
      setCtx(ev.text ?? S.ctx.text, S.ctx.diff, S.ctx.by, S.ctx.summary, false);
    }
    if (c) {
      c.body.append(h("div", { class: "sec" }, h("details", {},
        h("summary", { text: "what the model sees next (tool result)" }), h("pre", { class: "out", text: ev.observation }))));
    }
    counters();
  },

  reply(ev) {
    const w = S.work;
    const took = w ? `${dur(ev.t - w.t0)} · ${w.calls} LM call${w.calls === 1 ? "" : "s"} · ` +
      `${w.cmds} command${w.cmds === 1 ? "" : "s"}` : "";
    const task = ev.mode === "task";
    if (task) S.opsDone = (S.opsDone || 0) + 1;
    const done = task ? `Op ${S.opsDone} of ${ev.total} done` : "Done";
    const el = chat("assistant", ev.text || (task ? "(handled the op; asked for the next one)" : "(empty reply)"), "CLM");
    el.append(h("span", { class: "done" }, h("b", { text: `✓ ${done}` }), took ? ` · ${took}` : ""));
    S.work = null; tickWork();
    timeline(h("div", { class: "event reply" }, task
      ? `echo READY_FOR_NEXT_OP: ${ev.delivered} of ${ev.total} ops delivered.`
      : "Final answer handed to the user (plain text, no tool call). Waiting for the next message."));
  },

  grade(ev) {
    const r = ev.rewards || {};
    const main = r.reward ?? r.score ?? Object.values(r).find((v) => typeof v === "number");
    const rows = Object.entries(r).map(([k, v]) => `${k}: ${typeof v === "number" ? +v.toFixed(4) : v}`);
    const el = chat("assistant grade", rows.join("\n") || "(no rewards)", "ContextBench grade");
    const sg = r.single_guess;
    if (sg != null) {
      // Ours (clm_ui/metrics.py): only queries answered once, correctly, count.
      el.prepend(h("div", { class: "score single" }, `single_guess ${(100 * sg).toFixed(1)}%`,
        h("span", { class: "hint", text: ` · ${fmt(r.n_multi_guess)} queries answered more than once` })));
    }
    if (main != null) el.prepend(h("div", { class: "score" }, `${(100 * main).toFixed(1)}%`,
      sg != null ? h("span", { class: "hint", text: " reward (any guess counts)" }) : null));
    timeline(h("div", { class: "event end" }, "Graded by ContextBench (contextbench.harbor.reward): ", rows.join(", ")));
    $("session-info").textContent += (main != null ? ` · score ${(100 * main).toFixed(1)}%` : "") +
      (sg != null ? ` · single_guess ${(100 * sg).toFixed(1)}%` : "");
  },

  fs(ev) { S.roots = ev.roots; renderTree(); },

  finished(ev) {
    S.work = null; tickWork();
    const u = ev.usage || {};
    const txt = `Run ${ev.reason}.` + (ev.usage ? ` LM calls ${fmt(u.n_lm_calls)}, commands ${fmt(u.n_bash)}, ` +
      `edits ${fmt(u.n_ctx_syncs_real)} (rejected ${fmt(u.n_ctx_rejected)}), nudges ${fmt(u.n_nudges)}, ` +
      `rollbacks ${fmt(u.n_retry_on_limit)}, prompt tokens ${fmt(u.prompt_tokens)}.` : "");
    timeline(h("div", { class: "event end" }, txt));
    chat("system", txt);
  },

  error(ev) {
    timeline(h("div", { class: "event err" }, ev.message, ev.traceback ? h("details", {},
      h("summary", { text: "traceback" }), h("pre", { text: ev.traceback })) : null));
    chat("system", "Error: " + ev.message);
    $("status").dataset.state = "error"; $("status").textContent = "error";
  },
};

function handle(ev) {
  const f = H[ev.type];
  if (f) f(ev);
}

// ------------------------------------------------------------------ chat
function chat(cls, text, tag) {
  const log = $("chat-log");
  const el = h("div", { class: "msg " + cls }, tag ? h("span", { class: "tag", text: tag }) : null, text);
  log.append(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

const dur = (s) => (s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);

// The chat's progress line while the agent works on a message.
function tickWork() {
  const w = S.work, el = $("working");
  if (!w) { el.hidden = true; return; }
  const st = $("status").dataset.state;
  const doing = st === "running" ? `running: ${firstLine(S.lastCmd)}` : st === "thinking" ? "thinking" : "working";
  el.hidden = false;
  el.textContent = `Working on your message · ${dur(Date.now() / 1000 - w.t0)} · ${w.calls} LM calls · ` +
    `${w.cmds} commands · ${doing}`;
}
setInterval(() => S && S.work && tickWork(), 1000);

function updateComposer(state) {
  const input = $("input"), btn = $("btn-send");
  const live = S.session && state !== "finished" && $("status").dataset.state !== "finished";
  input.disabled = btn.disabled = !live;
  if (S.mode === "task") input.disabled = btn.disabled = true;
  if (!S.session) input.placeholder = "Start a session first";
  else if (S.mode === "task") input.placeholder = "The task's stream sends the ops; nothing to type.";
  else input.placeholder = "Message CLM";
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  send({ type: "send", text });
  $("input").value = "";
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("composer").requestSubmit(); }
});

// ------------------------------------------------------------------ timeline
function newCard(title) {
  const head = h("div", { class: "card-head" }, h("b", { text: title }));
  const meta = h("span", { class: "meta" });
  head.append(meta);
  const body = h("div");
  const el = h("div", { class: "card" }, head, body);
  timeline(el);
  S.card = { el, head, body, meta };
  return S.card;
}

// Scrolled to (near) the end: new content keeps it there. Scrolled up to read: left alone.
const atBottom = (el) => el.scrollHeight - el.scrollTop - el.clientHeight < 40;

function timeline(el) {
  const tl = $("timeline");
  const stick = atBottom(tl);
  tl.append(el);
  if (stick) requestAnimationFrame(() => (tl.scrollTop = tl.scrollHeight));
}

function miniDiff(diff) {
  if (!diff) return null;
  const rows = [];
  let shown = 0;
  for (const op of diff) {
    if (op.op === "eq") continue;
    for (const l of op.lines) {
      if (shown++ >= 60) break;
      rows.push(h("div", { class: "ln " + op.op, text: (op.op === "add" ? "+ " : "- ") + l }));
    }
  }
  if (!rows.length) return null;
  const total = diff.filter((o) => o.op !== "eq").reduce((a, o) => a + o.lines.length, 0);
  return h("details", { open: total <= 24 }, h("summary", { text: `${total} changed line(s)` }),
    h("div", { class: "minidiff scroll" }, rows));
}

$("ctx-only").addEventListener("change", (e) => $("timeline").classList.toggle("ctx-only", e.target.checked));

// ------------------------------------------------------------------ gauge + map
function gauge(tokens, limit, budget, ratios) {
  const g = $("gauge"), fill = $("gauge-fill");
  if (!limit) { $("gauge-num").textContent = `${fmt(tokens)} tok (no budget)`; return; }
  const pct = Math.min(100, (100 * tokens) / limit);
  fill.style.width = pct + "%";
  fill.className = "gauge-fill" + (pct >= 90 ? " bad" : pct >= 70 ? " warn" : "");
  $("gauge-num").textContent = `${fmt(tokens)} / ${fmt(limit)} (${Math.round(pct)}%)`;
  g.querySelectorAll(".gauge-tick").forEach((t) => t.remove());
  for (const r of ratios || []) {
    const x = Math.min(100, (100 * r * budget) / limit);
    g.append(h("div", { class: "gauge-tick", style: `left:${x}%`, title: `nudge at ${Math.round(r * 100)}% of the budget` }));
  }
}

let tip;
function ctxmap(turns) {
  const map = $("ctxmap");
  const prev = new Set([...map.children].map((c) => c.dataset.key));
  map.replaceChildren();
  for (const t of turns) {
    const key = `${t.label}|${t.tokens}`;
    const seg = h("div", {
      class: `seg ${t.pinned ? "pinned" : t.role}${prev.size && !prev.has(key) ? " fresh" : ""}`,
      style: `flex-grow:${Math.max(t.tokens, 1)}`, "data-key": key,
      onmouseenter: (e) => showTip(e, t), onmousemove: moveTip, onmouseleave: hideTip,
      onclick: () => jumpToTurn(t),
    });
    map.append(seg);
  }
}
function showTip(e, t) {
  hideTip();
  tip = h("div", { class: "tip" }, h("b", { text: `${t.label} · ${t.role}` }), ` · ${fmt(t.tokens)} tokens`,
    t.pinned ? h("div", { class: "hint", text: "Pinned by the harness. Not in the context file; the model cannot edit it." }) : null,
    h("pre", { text: t.preview }));
  document.body.append(tip); moveTip(e);
}
function moveTip(e) {
  if (!tip) return;
  tip.style.left = Math.min(e.clientX + 12, innerWidth - 400) + "px";
  tip.style.top = e.clientY + 16 + "px";
}
function hideTip() { if (tip) tip.remove(); tip = null; }

function jumpToTurn(t) {
  if (!S.ctx.path) return;
  select({ where: "sandbox", path: S.ctx.path });
  if (t.pinned) { openPrefix(t.index === 0 ? "system" : "task"); return; }
  const n = t.index - S.protect + 1;
  const el = document.getElementById(`ctx-turn-${n}`);
  if (el) {
    document.querySelectorAll(".ln.target").forEach((x) => x.classList.remove("target"));
    el.classList.add("target");
    el.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function counters() {
  const c = S.counters;
  const item = (label, v, title) => h("span", { title }, `${label} `, h("b", { text: fmt(v) }));
  $("counters").replaceChildren(...[
    item("LM calls", c.calls), item("commands", c.cmds),
    item("edits applied", c.applied), item("nudges", c.nudges)]);
}

// ------------------------------------------------------------------ file tree
function renderTree() {
  const tree = $("tree");
  const changed = new Set();
  const next = new Map();
  for (const r of S.roots) for (const e of r.entries) {
    const key = `${r.where}:${e.path}`;
    if (S.mtimes.has(key) && S.mtimes.get(key) !== e.mtime && e.kind === "f") changed.add(key);
    if (!S.mtimes.has(key) && S.mtimes.size && e.kind === "f") changed.add(key);
    next.set(key, e.mtime);
  }
  S.mtimes = next;
  tree.replaceChildren();
  for (const r of S.roots) {
    const box = h("div", { class: "root" }, h("div", { class: "root-head" },
      h("b", { text: r.label }), h("span", { class: "path", title: r.path }, h("bdi", { text: r.path })),
      h("span", { class: "about", text: r.about })));
    const base = r.path.split("/").length;
    if (!r.entries.length) box.append(h("div", { class: "node dir" }, h("span", { class: "nm",
      text: r.empty ? `(empty: ${r.empty})` : "(empty)" })));
    for (const e of r.entries) {
      const parts = e.path.split("/");
      const depth = parts.length - base - 1;
      const key = `${r.where}:${e.path}`;
      const isCtx = e.path === S.ctx.path;
      const sel = S.selected && S.selected.where === r.where && S.selected.path === e.path;
      box.append(h("div", {
        class: `node ${e.kind === "d" ? "dir" : ""} ${isCtx ? "ctx" : ""} ${sel ? "sel" : ""} ${changed.has(key) ? "flash" : ""}`,
        style: `padding-left:${14 + depth * 14}px`, title: e.path,
        onclick: e.kind === "d" ? null : () => select({ where: r.where, path: e.path }),
      }, h("span", { class: "nm", text: parts[parts.length - 1] + (e.kind === "d" ? "/" : "") }),
        e.kind === "f" ? h("span", { class: "sz", text: kb(e.size) }) : null));
    }
    tree.append(box);
  }
  if (S.selected && !isCtxSelected() && changed.has(`${S.selected.where}:${S.selected.path}`)) loadFile();
}

// ------------------------------------------------------------------ viewer
const isCtxSelected = () => S.selected && S.selected.where === "sandbox" && S.selected.path === S.ctx.path;

function select(sel) {
  S.selected = sel;
  // <bdi>: the box is right-to-left so long paths lose their start, not their end.
  $("viewer-path").replaceChildren(h("bdi", { text: sel.path }));
  $("viewer-path").title = sel.path;
  renderTree();
  if (isCtxSelected()) renderViewer(); else loadFile();
}

async function loadFile() {
  const sel = S.selected; if (!sel) return;
  $("viewer-live").hidden = true;
  $("viewer-sub").replaceChildren();
  try {
    const r = await fetch(`/api/file?where=${encodeURIComponent(sel.where)}&path=${encodeURIComponent(sel.path)}`);
    if (!r.ok) throw new Error("not readable");
    let { text } = await r.json();
    if (sel.path.endsWith(".json")) { try { text = JSON.stringify(JSON.parse(text), null, 2); } catch {} }
    if (sel !== S.selected) return;
    if (sel.path.includes("context_snapshots/")) {
      $("viewer-sub").textContent = "The exact message list sent to the model for one LM call (ContextBench's verifier grades these).";
    }
    $("viewer-body").replaceChildren(...splitLines(text).map((l) => h("div", { class: "ln", text: l })));
  } catch (err) {
    $("viewer-body").replaceChildren(h("div", { class: "ln", text: `(${err.message})` }));
  }
}

function setCtx(text, diff, by, summary, live = false) {
  Object.assign(S.ctx, { text: text ?? S.ctx.text, diff, by, summary, live });
  if (isCtxSelected()) renderViewer(by === "model" ? "change" : "end");
}

// At most one render per frame: a reconnecting page replays the whole session at once.
let pendingScroll, pendingFrame = 0;
function renderViewer(scroll) {
  if (scroll) pendingScroll = scroll;
  if (pendingFrame) return;
  pendingFrame = requestAnimationFrame(() => {
    pendingFrame = 0;
    const sc = pendingScroll; pendingScroll = undefined;
    drawViewer(sc);
  });
}

function drawViewer(scroll) {
  const body = $("viewer-body"), ctx = S.ctx;
  const wasAtBottom = atBottom(body);
  $("viewer-live").hidden = !ctx.live;
  // Context sub-header: what this file is and what is not in it.
  const editable = S.ctxTurns.filter((t) => !t.pinned);
  const pinned = S.ctxTurns.filter((t) => t.pinned);
  const sub = $("viewer-sub");
  // The harness writes the file only right before a bash command. Before the first
  // command it does not exist, so show what it will contain; after a command, turns
  // appended since (the command's own turn, a plain-text answer) are not in it yet.
  const preview = !ctx.written;
  const text = preview ? S.ctxPreview || "" : ctx.text;
  const turnsIn = (s) => (s.match(/^\[\[CTX_TURN \d+ role=\w+\]\]$/gm) || []).length;
  const behind = ctx.written && S.ctxPreview != null ? turnsIn(S.ctxPreview) - turnsIn(ctx.text) : 0;
  const status = preview
    ? h("div", { class: "notice info" }, h("b", { text: "Not written yet. " }),
      "The harness writes this file only right before a bash command, and the model has not run one " +
      "(a plain-text answer runs no command). Below is what the file will contain at the next command.")
    : behind > 0
      ? h("div", { class: "hint" }, `${behind} newer turn(s) are in the context but not in the file yet; ` +
        "the harness rewrites the file before the next command.")
      : null;
  sub.replaceChildren(...[
    h("div", {}, h("b", { text: "One file holds the whole editable context: " }),
      `${editable.length} turns, ${fmt(editable.reduce((a, t) => a + t.tokens, 0))} tokens, one [[CTX_TURN i role=…]] block per message. ` +
      "Not in the file, pinned by the harness:",
      pinned.length ? pinned.map((t) => h("button", { class: "ghost small", onclick: () => openPrefix(t.index === 0 ? "system" : t.index === 1 ? "task" : "ledger") },
        `${t.label} · ${fmt(t.tokens)} tok`)) : " system prompt, task"),
    status,
    !preview && ctx.summary ? h("div", {}, ctx.summary) : null,
    h("div", { class: "legend-line" },
      h("span", {}, h("i", { style: "background:var(--add-bg);border:1px solid var(--ok)" }), "added by the model"),
      h("span", {}, h("i", { style: "background:var(--del-bg);border:1px solid var(--bad)" }), "removed by the model"),
      h("span", {}, h("i", { style: "background:var(--harness-bg);border:1px solid var(--assistant)" }), "written by the harness")),
  ].filter(Boolean));
  if (S.openPrefix) sub.append(h("pre", { class: "prefix-text", text: prefixText(S.openPrefix) }));

  const lines = splitLines(text);
  const rows = [];
  const showDiff = !preview && ctx.diff;
  const push = (t, cls) => {
    const m = HEADER_RE.exec(t);
    const row = h("div", { class: `ln ${cls}${m ? " hdr r-" + m[2] : ""}`, text: t });
    if (m) row.id = `ctx-turn-${m[1]}`;
    rows.push(row);
  };
  let firstChange = null;
  if (!showDiff) lines.forEach((l) => push(l, ""));
  else {
    let j = 0;
    const model = ctx.by === "model";
    for (const op of ctx.diff) {
      if (op.op === "eq") { for (let k = 0; k < op.n; k++) push(lines[j++] ?? "", ""); continue; }
      if (firstChange == null) firstChange = rows.length;
      if (op.op === "add") for (const l of op.lines) { push(lines[j++] ?? l, model ? "add" : "hadd"); }
      else for (const l of op.lines) push(l, model ? "del" : "hdel");
    }
    while (j < lines.length) push(lines[j++], "");
  }
  if (!rows.length) rows.push(h("div", { class: "ln", text: "(empty: no editable turns yet)" }));
  body.replaceChildren(...rows);
  if (scroll === "change" && firstChange != null) rows[firstChange].scrollIntoView({ block: "center" });
  else if (scroll === "end" && wasAtBottom) body.scrollTop = body.scrollHeight;
}

function prefixText(which) {
  if (which === "ledger") {
    const t = S.ctxTurns.find((x) => x.label === "rollback ledger");
    return t ? t.preview : "";
  }
  return S.prefix ? S.prefix[which] : "(not sent yet)";
}
function openPrefix(which) {
  S.openPrefix = S.openPrefix === which ? null : which;
  renderViewer();
}


// ------------------------------------------------------------------ dialog
const LS_KEY = "clm-ui-config";
let MODELS = [], TASKS = [];
const perM = (p) => (p == null || p === "" ? null : Number(p) * 1e6);  // $ per 1M tokens
async function openDialog() {
  const form = $("cfg-form");
  let cfg = {};
  try {
    const d = await (await fetch("/api/defaults")).json();
    cfg = d.config;
    $("sandbox-kind").value = d.sandbox;
  } catch {}
  try {
    // The last settings used here, minus values an older version of the page saved.
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    if (!["chat", "task"].includes(saved.mode)) delete saved.mode;
    if (!saved.model || saved.model === "mock" || !saved.model.includes("/")) delete saved.model;
    Object.assign(cfg, saved);
  } catch {}
  for (const el of form.elements) if (el.name && cfg[el.name] != null && el.name !== "task") el.value = cfg[el.name];
  modeHint();
  $("dlg").showModal();
  // Catalogs load after the dialog opens: OpenRouter's list takes a moment.
  try {
    const t = await (await fetch("/api/tasks")).json();
    TASKS = t.tasks || [];
    const byFamily = {};
    for (const x of TASKS) (byFamily[x.family || "other"] ||= []).push(x);
    $("task-select").replaceChildren(...(TASKS.length ? Object.entries(byFamily).map(([fam, xs]) =>
      h("optgroup", { label: fam }, xs.map((x) => h("option", { value: x.name },
        `${x.name} · ${x.ops ?? "?"} ops${x.regime ? " · " + x.regime : ""}`)))) :
      [h("option", { value: "", text: "no tasks found (start clm-ui with --tasks-dir)" })]));
    if (cfg.task && TASKS.some((x) => x.name === cfg.task)) $("task-select").value = cfg.task;
    taskHint();
  } catch {}
  try {
    const m = await (await fetch("/api/models")).json();
    MODELS = m.models || [];
    $("model-list").replaceChildren(...MODELS.map((x) => h("option", { value: x.id }, x.name)));
    $("model-hint").textContent = MODELS.length ? "" : `Could not load OpenRouter's model list (${m.error || "?"}); type a model id.`;
  } catch {}
  modelHint();
}
function modelHint() {
  const id = $("cfg-form").elements.model.value.trim(), m = MODELS.find((x) => x.id === id);
  if (!MODELS.length) return;
  $("model-hint").textContent = !id ? `${MODELS.length} OpenRouter models support tool calls; type to search.`
    : m ? `${m.name} · context ${fmt(m.context_length)} · $${perM(m.pricing.prompt)?.toFixed(2)} in / ` +
      `$${perM(m.pricing.completion)?.toFixed(2)} out per 1M tokens`
      : "Not in OpenRouter's list of models with tool support: CLM needs tool calls.";
}
function taskHint() {
  const t = TASKS.find((x) => x.name === $("task-select").value);
  $("task-hint").textContent = t ? `${t.family} · ${t.ops} ops streamed one at a time · ` +
    `~${fmt(t.input_tokens)} input tokens in all, against a 32,768-token budget` : "";
}
function modeHint() {
  const task = $("cfg-form").elements.mode.value === "task";
  document.querySelectorAll(".task-only").forEach((x) => (x.hidden = !task));
  document.querySelectorAll(".chat-only").forEach((x) => (x.hidden = task));
  $("task-select").required = task;
  $("mode-hint").textContent = task
    ? "Runs the task as harbor trials start -a clm_contextbench:ClmStream does: the task's own sandbox and " +
      "op stream, the paper's settings and method skill. The ops arrive automatically; the run is graded " +
      "with ContextBench's grader at the end."
    : "ClmAgent's defaults (nudges at 25/50/75%, adaptive urgent nudge, rollback-retry), no step limit. " +
      "The agent works with bash and answers in plain text.";
}
$("cfg-form").elements.mode.addEventListener("change", modeHint);
$("cfg-form").elements.model.addEventListener("input", modelHint);
$("task-select").addEventListener("change", taskHint);
$("dlg").addEventListener("close", () => {
  if ($("dlg").returnValue !== "ok") return;
  const f = $("cfg-form"), cfg = {};
  for (const el of f.elements) if (el.name) cfg[el.name] = el.value;
  try { localStorage.setItem(LS_KEY, JSON.stringify(cfg)); } catch {}
  send({ type: "new_session", config: cfg });
});
$("btn-new").addEventListener("click", openDialog);
$("btn-stop").addEventListener("click", () => send({ type: "stop" }));

// ------------------------------------------------------------------ reset
function resetUI() {
  S = fresh();
  for (const id of ["chat-log", "timeline", "tree", "viewer-body", "viewer-sub", "ctxmap"]) $(id).replaceChildren();
  $("session-info").textContent = "no session";
  $("status").dataset.state = "idle"; $("status").textContent = "idle";
  $("banner").hidden = true; $("working").hidden = true;
  $("gauge-fill").style.width = "0"; $("gauge-num").textContent = "–";
  $("viewer-path").textContent = "select a file";
  counters(); updateComposer();
}

resetUI();
connect();
