# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Benchmark: the same ContextBench tasks under several setups ("arms"), side by side.

An arm is an agent (``clm``: ``ClmStream``; ``baseline``: the base model without CLM,
ContextBench's append-only agent), an OpenRouter model, ContextBench's method text and a
steering skill. Every arm runs every task ``reps`` times; each run is one
``python -m clm_ui.episode`` process, the task runner the evolution loop uses, and up to
``workers`` of them run at once.

For each arm (overall and per task) the page shows the mean and standard error of the
score (ContextBench's ``reward``, and ``single_guess`` where the family has it), peak
context tokens (the grader's ``agent_peak_tokens``), context edits (CLM's non-trivial
edits, ``n_ctx_syncs_real``; the baseline makes none), cost (USD at OpenRouter's prices)
and wall time. While runs go, each running episode's ``events.jsonl`` is followed, so the
page can show them all at once: ops delivered, context tokens per turn, edits, the last
command.

``workers`` runs start together, each creating its own sandbox at the same time, and each
deletes its sandbox before its process exits: ``workers`` sandboxes exist at once.

A benchmark lives in ``<benchmarks_dir>/<name>/``: ``config.json``, ``benchmark.log``,
``skills/<arm>/skill/SKILL.md`` (each arm's steering text), ``trials/<arm>/<task>__r<k>/``
(every run's folder) and ``results/<arm>/<task>__r<k>.json`` (its numbers). Starting a
benchmark with an existing name resumes it: graded runs are kept, the rest rerun.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .episode import run_process, stop_process, usage_cost
from .session import AGENTS, METHOD_SKILLS
from .skills import mount_dir

LOG_FILE = "benchmark.log"
CONFIG_FILE = "config.json"
#: Points of the context-tokens series sent to the page per run.
MAX_SERIES = 160
#: Recent events kept per run for the page's live feed.
FEED_LEN = 80

#: Arms for "CLM vs the base model", the comparison the view offers first.
PRESET_CLM_VS_BASE = [
    {"label": "clm", "agent": "clm", "method_skill": "clm"},
    {"label": "base", "agent": "baseline", "method_skill": "base"},
]

#: Metrics summarized per arm: key in a run's result -> what the page calls it.
METRICS = ("score", "single_guess", "peak_tokens", "edits", "cost", "duration_s",
           "n_lm_calls", "ops_frac")


def _slug(s: str) -> str:
    return "".join(c for c in str(s).strip() if c.isalnum() or c in "-_.").strip(".")


@dataclass
class Arm:
    label: str
    agent: str = "clm"          # clm | baseline
    model: str = ""             # OpenRouter id; "" = the benchmark's model
    method_skill: str = "clm"   # ContextBench's method text: clm | base | none
    steering: str = ""          # a skill library id, "" = none

    def describe(self) -> str:
        who = "CLM" if self.agent == "clm" else "base model, no CLM"
        steer = f", steered by {self.steering}" if self.steering else ""
        return f"{who} · {self.model} · method {self.method_skill}{steer}"


@dataclass
class BenchmarkConfig:
    name: str
    model: str                          # default OpenRouter model for the arms
    tasks: list[str]
    arms: list[Arm] = field(default_factory=list)
    reps: int = 3
    workers: int = 4
    episode_timeout_s: float = 3600
    # Paired: every arm's run of one (task, repeat) starts together, and the next pair
    # starts only once the whole pair has finished, so the arms always run side by side.
    paired: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BenchmarkConfig":
        missing = [k for k in ("name", "model", "tasks") if not d.get(k)]
        if missing:
            raise ValueError(f"missing: {', '.join(missing)}")
        name = _slug(d["name"])
        if not name:
            raise ValueError("give the benchmark a name (letters, digits, - _ .)")
        model = str(d["model"]).strip().removeprefix("openrouter/")
        arms, seen = [], set()
        for a in d.get("arms") or PRESET_CLM_VS_BASE:
            a = a if isinstance(a, dict) else asdict(a)
            arm = Arm(**{k: v for k, v in a.items() if k in Arm.__dataclass_fields__})
            arm.label = _slug(arm.label) or f"arm{len(arms) + 1}"
            arm.model = str(arm.model or model).strip().removeprefix("openrouter/")
            arm.steering = str(arm.steering or "").strip()
            if arm.agent not in AGENTS:
                raise ValueError(f"arm {arm.label}: agent must be one of {AGENTS}")
            if arm.method_skill not in METHOD_SKILLS:
                raise ValueError(f"arm {arm.label}: method text must be one of {METHOD_SKILLS}")
            if arm.label in seen:
                raise ValueError(f"two arms are called {arm.label!r}")
            seen.add(arm.label)
            arms.append(arm)
        if not arms:
            raise ValueError("add at least one arm")
        tasks = [str(t) for t in d["tasks"]]
        if any(_slug(t) != t for t in tasks):
            raise ValueError("bad task name")
        cfg = cls(name=name, model=model, tasks=tasks, arms=arms,
                  reps=int(d.get("reps", 3)), workers=int(d.get("workers", 4)),
                  episode_timeout_s=float(d.get("episode_timeout_s", 3600)),
                  paired=str(d.get("paired", True)).lower() not in ("false", "0", "off", ""))
        if cfg.reps < 1 or cfg.workers < 1:
            raise ValueError("repeats and runs in parallel must be at least 1")
        return cfg

    @classmethod
    def load(cls, d: dict[str, Any]) -> "BenchmarkConfig":
        return cls.from_dict({**d, "arms": d.get("arms") or []})

    def cells(self) -> list[tuple[str, str, int]]:
        """(arm, task, rep) for every run, repeat-major and arms interleaved, so every
        arm advances at the same pace and a stopped benchmark is still balanced."""
        return [(a.label, t, r) for r in range(self.reps) for t in self.tasks
                for a in self.arms]

    def n_runs(self) -> int:
        return len(self.arms) * len(self.tasks) * self.reps


def cell_id(arm: str, task: str, rep: int) -> str:
    return f"{arm}/{task}__r{rep}"


# ----------------------------------------------------------------- following a run live
class Tail:
    """Follows one run's ``events.jsonl`` from where it last read, keeping only what the
    page shows: the state, ops, counters, the context-tokens series and recent events."""

    def __init__(self, path: Path) -> None:
        self.path, self.offset, self.rest = path, 0, b""
        self.lock = threading.Lock()
        self.state = "queued"
        self.ops: dict[str, Any] = {}
        self.tokens: list[int] = []
        self.limit: int | None = None
        self.calls = self.cmds = self.edits = 0
        self.last_command = ""
        self.feed: deque[dict[str, Any]] = deque(maxlen=FEED_LEN)
        self.grade: dict[str, Any] | None = None

    def update(self) -> "Tail":
        with self.lock:
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(self.offset)
                    data = fh.read()
            except OSError:
                return self
            self.offset += len(data)
            *lines, self.rest = (self.rest + data).split(b"\n")
            for raw in lines:
                try:
                    self._take(json.loads(raw))
                except ValueError:
                    continue
        return self

    def _feed(self, ev: dict[str, Any], kind: str, text: str) -> None:
        self.feed.append({"t": ev.get("t"), "kind": kind, "text": text[:600]})

    def _take(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "status":
            self.state = ev.get("state", self.state)
        elif kind == "budget":
            self.tokens.append(int(ev.get("tokens") or 0))
            self.limit = ev.get("limit") or self.limit
            if ev.get("notices"):
                self._feed(ev, "notice", " ".join(ev["notices"]))
        elif kind == "llm_response":
            self.calls += 1
            if ev.get("content"):
                self._feed(ev, "thought", ev["content"])
        elif kind == "command_start":
            self.cmds += 1
            self.last_command = ev.get("command") or ""
            self._feed(ev, "command", self.last_command)
        elif kind == "ctx_edit" and ev.get("applied"):
            self.edits += 1
            before, after = ev.get("before"), ev.get("after")
            self._feed(ev, "edit", f"context edit {ev.get('status')}" +
                       (f": {before:,} → {after:,} tokens" if before and after else ""))
        elif kind == "rollback":
            self._feed(ev, "notice", f"rollback: dropped {ev.get('dropped')} turn(s)")
        elif kind == "user_message" and ev.get("kind") == "op":
            if ev.get("total"):
                self.ops = {"delivered": ev.get("delivered"), "total": ev.get("total")}
            self._feed(ev, "op", ev.get("text") or "")
        elif kind == "reply":
            if ev.get("total"):
                self.ops = {"delivered": ev.get("delivered"), "total": ev.get("total")}
            if ev.get("text"):
                self._feed(ev, "reply", ev["text"])
        elif kind == "grade":
            self.grade = ev.get("rewards")
            self._feed(ev, "grade", f"reward {self.grade.get('reward')}")
        elif kind == "error":
            self._feed(ev, "error", ev.get("message") or "")
        elif kind == "finished":
            self._feed(ev, "finished", f"{ev.get('reason')} ({ev.get('terminated_by') or '–'})")

    def summary(self) -> dict[str, Any]:
        step = max(1, math.ceil(len(self.tokens) / MAX_SERIES))
        series = self.tokens[::step]
        if self.tokens and (len(self.tokens) - 1) % step:
            series.append(self.tokens[-1])   # always end on the latest turn
        return {"state": self.state, "ops": self.ops, "limit": self.limit, "series": series,
                "tokens": self.tokens[-1] if self.tokens else None,
                "calls": self.calls, "cmds": self.cmds, "edits": self.edits,
                "last_command": self.last_command[:300]}


_TAILS: dict[Path, Tail] = {}
_TAILS_LOCK = threading.Lock()


def tail(path: Path) -> Tail:
    with _TAILS_LOCK:
        t = _TAILS.get(path)
        if t is None:
            t = _TAILS[path] = Tail(path)
    return t.update()


# ----------------------------------------------------------------------------- the job
class BenchmarkJob:
    """Runs every (arm, task, rep) of a benchmark in a background thread, ``workers``
    episode processes at a time. One at a time per server."""

    def __init__(self, cfg: BenchmarkConfig, run_dir: Path, *, tasks_dir: Path, sandbox: str,
                 skills: Any, pricing: dict[str, dict[str, Any] | None]) -> None:
        self.cfg, self.dir = cfg, run_dir
        self.tasks_dir, self.sandbox, self.skills = tasks_dir, sandbox, skills
        self.pricing = pricing                    # model id -> OpenRouter pricing
        self.status = "starting"
        self.error: str | None = None
        self.stop_requested = False
        self.running_cells: set[str] = set()
        self._procs: set[Any] = set()
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._main, name=f"benchmark-{cfg.name}",
                                       daemon=True)

    def track(self, proc: Any) -> None:
        with self._lock:
            self._procs.add(proc)

    def untrack(self, proc: Any) -> None:
        with self._lock:
            self._procs.discard(proc)

    def stop(self, wait: bool = False) -> None:
        """End the running episodes gracefully: each deletes its sandbox, then exits.
        ``wait``: return only once they have (server shutdown); else in the background."""
        self.stop_requested = True
        self.status = "stopping"
        with self._lock:
            procs = list(self._procs)
        threads = [threading.Thread(target=stop_process, args=(p,), daemon=True) for p in procs]
        for t in threads:
            t.start()
        if wait:
            for t in threads:
                t.join()

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def start(self) -> None:
        for t in self.cfg.tasks:
            self._task_dir(t)          # refuses a task the tasks folder does not hold
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / CONFIG_FILE).write_text(json.dumps(asdict(self.cfg), indent=2) + "\n",
                                            encoding="utf-8")
        self.thread.start()

    def _log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(f"[benchmark] {line}", flush=True)
        with self._lock, open(self.dir / LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _task_dir(self, name: str) -> Path:
        d = (self.tasks_dir / name).resolve()
        if not d.is_relative_to(self.tasks_dir.resolve()) or not (d / "task.toml").is_file():
            raise ValueError(f"no task {name!r} in {self.tasks_dir}")
        return d

    def command(self, arm: Arm, task: str, rep: int) -> list[str]:
        """The run's process: ``python -m clm_ui.episode`` for one task under one arm."""
        cmd = [sys.executable, "-m", "clm_ui.episode", "--task", str(self._task_dir(task)),
               "--model", arm.model, "--sandbox", self.sandbox,
               "--out", str(self.dir / "trials" / arm.label), "--name", f"{task}__r{rep}",
               "--method-skill", arm.method_skill, "--agent", arm.agent]
        skill = self.dir / "skills" / arm.label / "skill"
        if (skill / "SKILL.md").is_file():
            cmd += ["--skill-dir", str(skill)]
        return cmd

    def _main(self) -> None:
        cfg = self.cfg
        try:
            self.status = "running"
            arms = {a.label: a for a in cfg.arms}
            for a in cfg.arms:   # each arm's steering text, kept with the benchmark
                if a.steering:
                    mount_dir(self.dir / "skills" / a.label, self.skills.text(a.steering))
            todo = [c for c in cfg.cells() if not _graded(self.dir, *c)]
            self._log(f"benchmark {cfg.name}: {len(cfg.arms)} arms x {len(cfg.tasks)} tasks x "
                      f"{cfg.reps} repeats = {cfg.n_runs()} runs, {len(todo)} to run, "
                      f"{cfg.workers} at a time" + (", in pairs" if cfg.paired else ""))
            run = lambda c: self._run(arms[c[0]], c[1], c[2])  # noqa: E731
            if cfg.paired:
                self._run_pairs(todo, run)
            else:
                with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
                    for _ in pool.map(run, todo):
                        pass
            if self.stop_requested:
                self.status = "stopped"
                self._log("stopped; start it again with the same name to resume")
            else:
                self.status = "done"
                self._log("done")
        except BaseException as exc:  # noqa: BLE001
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            self._log(f"error: {self.error}\n{traceback.format_exc()}")

    def _run_pairs(self, todo: list[tuple[str, str, int]], run: Any) -> None:
        """Pairs side by side: a pair is the runs of one (task, repeat), one per arm. Up
        to ``workers // arms`` pairs run at once (at least one); a pair's slot frees only
        when all its runs have finished."""
        pairs: dict[tuple[str, int], list[tuple[str, str, int]]] = {}
        for c in todo:
            pairs.setdefault((c[1], c[2]), []).append(c)
        slots = max(1, self.cfg.workers // len(self.cfg.arms))

        def run_pair(cells: list[tuple[str, str, int]]) -> None:
            with ThreadPoolExecutor(max_workers=min(len(cells), self.cfg.workers)) as pool:
                list(pool.map(run, cells))

        with ThreadPoolExecutor(max_workers=slots) as pool:
            list(pool.map(run_pair, pairs.values()))

    def _run(self, arm: Arm, task: str, rep: int) -> None:
        if self.stop_requested:
            return
        cid = cell_id(arm.label, task, rep)
        run_dir = self.dir / "trials" / arm.label / f"{task}__r{rep}"
        if run_dir.exists():   # a rerun starts clean: old events would mix into the live view
            shutil.rmtree(run_dir, ignore_errors=True)
            with _TAILS_LOCK:
                _TAILS.pop(run_dir / "events.jsonl", None)
        self.running_cells.add(cid)
        self._log(f"start {cid}")
        try:
            result = run_process(self.command(arm, task, rep),
                                 self.dir / "logs" / arm.label / f"{task}__r{rep}.log",
                                 self.cfg.episode_timeout_s, self.track, self.untrack)
        finally:
            self.running_cells.discard(cid)
        if self.stop_requested and result is None:
            return
        rec = record(arm, task, rep, result, self.pricing.get(arm.model), run_dir)
        out = self.dir / "results" / arm.label / f"{task}__r{rep}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, indent=2, default=str) + "\n", encoding="utf-8")
        self._log(f"done  {cid}: score {rec['score']}" + (f" ({rec['error']})" if rec["error"] else ""))


def record(arm: Arm, task: str, rep: int, result: dict | None, pricing: dict | None,
           run_dir: Path) -> dict[str, Any]:
    """One run's numbers, from the episode's result line and its run folder."""
    rec: dict[str, Any] = {"arm": arm.label, "task": task, "rep": rep, "run_dir": str(run_dir),
                           "score": None, "error": None}
    if result is None:
        rec["error"] = "the run ended without a result; see its log"
        return rec
    rewards = result.get("rewards") or {}
    usage = result.get("usage") or {}
    final = _read_json(Path(result.get("run_dir") or run_dir) / "result.json") or {}
    cost, metric = usage_cost(usage, pricing)
    delivered, total = rewards.get("n_ops_delivered"), rewards.get("n_ops_total")
    rec.update({
        "run_dir": result.get("run_dir") or str(run_dir),
        "score": rewards.get("reward"),
        "single_guess": rewards.get("single_guess"),
        "peak_tokens": rewards.get("agent_peak_tokens", usage.get("peak_context_tokens")),
        "edits": usage.get("n_ctx_syncs_real", 0 if arm.agent == "baseline" else None),
        "cost": cost, "cost_metric": metric,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "n_lm_calls": usage.get("n_lm_calls", usage.get("n_llm_calls")),
        "ops_delivered": delivered, "ops_total": total,
        "ops_frac": delivered / total if delivered is not None and total else None,
        "terminated_by": final.get("terminated_by"),
        "duration_s": final.get("duration_s"),
        "rewards": rewards,
        "error": None if rewards.get("reward") is not None else "not graded",
    })
    return rec


def _graded(bench_dir: Path, arm: str, task: str, rep: int) -> bool:
    rec = _read_json(bench_dir / "results" / arm / f"{task}__r{rep}.json")
    return bool(rec) and rec.get("score") is not None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------- statistics
def mean_se(xs: list[float]) -> dict[str, Any]:
    """Mean and standard error of the mean (sample sd / sqrt n; None for n < 2)."""
    xs = [float(x) for x in xs if x is not None]
    n = len(xs)
    if not n:
        return {"mean": None, "se": None, "n": 0}
    m = sum(xs) / n
    se = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n) if n > 1 else None
    return {"mean": m, "se": se, "n": n}


def summarize(recs: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [r for r in recs if r.get("score") is not None]
    out = {k: mean_se([r.get(k) for r in graded]) for k in METRICS}
    out["n_runs"], out["n_graded"] = len(recs), len(graded)
    out["cost_metric"] = next((r.get("cost_metric") for r in graded if r.get("cost_metric")), None)
    return out


def _diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
    """b - a, with the standard error of a difference of independent means."""
    if a["mean"] is None or b["mean"] is None:
        return None
    se = (math.sqrt(a["se"] ** 2 + b["se"] ** 2)
          if a["se"] is not None and b["se"] is not None else None)
    return {"mean": b["mean"] - a["mean"], "se": se}


# --------------------------------------------------------------------- reading a run
def list_runs(benchmarks_dir: Path) -> list[dict[str, Any]]:
    out = []
    if benchmarks_dir.is_dir():
        for d in sorted(benchmarks_dir.iterdir()):
            cfg = _read_json(d / CONFIG_FILE)
            if not cfg:
                continue
            n_done = sum(1 for _ in (d / "results").glob("*/*.json"))
            out.append({"name": d.name, "arms": [a["label"] for a in cfg.get("arms", [])],
                        "n_runs": len(cfg.get("arms", [])) * len(cfg.get("tasks", [])) *
                        int(cfg.get("reps", 1)), "n_done": n_done})
    return out


def snapshot(bench_dir: Path, job: BenchmarkJob | None = None) -> dict[str, Any]:
    """Everything the page shows about one benchmark, from its files, plus a live summary
    of each run in progress."""
    raw = _read_json(bench_dir / CONFIG_FILE) or {}
    cfg = BenchmarkConfig.load(raw) if raw else None
    mine = job is not None and job.dir == bench_dir
    running = set(job.running_cells) if mine else set()
    cells, by_arm, by_arm_task = [], {}, {}
    for arm, task, rep in (cfg.cells() if cfg else []):
        cid = cell_id(arm, task, rep)
        rec = _read_json(bench_dir / "results" / arm / f"{task}__r{rep}.json")
        state = ("running" if cid in running else "error" if rec and rec.get("score") is None
                 else "done" if rec else "queued")
        cell = {"id": cid, "arm": arm, "task": task, "rep": rep, "state": state,
                "score": rec.get("score") if rec else None,
                "error": rec.get("error") if rec else None}
        if state == "running":
            cell["live"] = tail(bench_dir / "trials" / arm / f"{task}__r{rep}" /
                                "events.jsonl").summary()
        cells.append(cell)
        if rec:
            by_arm.setdefault(arm, []).append(rec)
            by_arm_task.setdefault((arm, task), []).append(rec)
    arms = []
    for a in (cfg.arms if cfg else []):
        arms.append({**asdict(a), "about": a.describe(), "stats": summarize(by_arm.get(a.label, [])),
                     "tasks": {t: summarize(by_arm_task.get((a.label, t), []))
                               for t in cfg.tasks}})
    ref = arms[0] if arms else None
    for a in arms[1:]:   # each arm against the first, the reference
        a["vs_ref"] = {k: _diff(ref["stats"][k], a["stats"][k]) for k in ("score", "peak_tokens",
                                                                            "cost")}
    log_tail = ""
    if (bench_dir / LOG_FILE).is_file():
        log_tail = "\n".join((bench_dir / LOG_FILE).read_text(encoding="utf-8")
                             .splitlines()[-80:])
    status = job.status if mine else (
        "done" if cells and all(c["state"] == "done" for c in cells) else "idle")
    return {"name": bench_dir.name, "status": status, "error": job.error if mine else None,
            "config": raw, "arms": arms, "cells": cells, "n_runs": len(cells),
            "n_done": sum(c["state"] in ("done", "error") for c in cells), "log": log_tail}


def run_detail(bench_dir: Path, cid: str) -> dict[str, Any]:
    """One run of a benchmark: its live summary, recent events and numbers."""
    arm, _, name = cid.partition("/")
    if not arm or not name or "/" in name or ".." in cid:
        raise ValueError(f"bad run {cid!r}")
    run_dir = (bench_dir / "trials" / arm / name).resolve()
    if not run_dir.is_relative_to(bench_dir.resolve()):
        raise ValueError(f"bad run {cid!r}")
    t = tail(run_dir / "events.jsonl")
    with t.lock:
        feed = list(t.feed)
    return {"id": cid, "run_dir": str(run_dir), "live": t.summary(), "feed": feed,
            "result": _read_json(bench_dir / "results" / arm / f"{name}.json")}


def delete(bench_dir: Path) -> None:
    """Delete a benchmark's folder: its config, results, logs and every run's folder."""
    if not (bench_dir / CONFIG_FILE).is_file():
        raise ValueError(f"no benchmark {bench_dir.name!r}")
    shutil.rmtree(bench_dir)
    root = bench_dir.resolve()
    with _TAILS_LOCK:
        for path in [p for p in _TAILS if p.resolve().is_relative_to(root)]:
            del _TAILS[path]
