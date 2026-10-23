# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Evolve a steering skill on ContextBench tasks with ``clm_icl``, from the page.

The loop is ``clm_icl.evolve.Evolution``, unchanged: run the incumbent skill on the
training tasks, contrast better and worse runs, ask a proposer model for rewritten
skills, run them on the development tasks, keep one only if it beats the gate. This
module supplies what that loop needs:

* ``ContextBenchSource``, the ``TaskSource``: each episode is one ContextBench task run
  as the page runs it (``python -m clm_ui.episode``, in its own process), scored by
  ContextBench's ``reward`` or by ``single_guess``, with its cost in USD from
  OpenRouter's prices;
* ``EvolutionJob``: the loop in a background thread, stoppable and resumable;
* ``snapshot``: the run as the page shows it, read from the files the loop writes
  (``state.json``, ``rounds/r<k>/``, ``episodes/``) and ``evolution.log``.

A run lives in ``<evolutions_dir>/<name>/``; rerunning the same name resumes it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clm_icl import evolve as _evolve
from clm_icl.evolve import Evolution
from clm_icl.propose import litellm_proposer
from clm_icl.tasks import Episode, TaskSource

from .agent import line_diff
from .episode import run_process, stop_process, usage_cost
from .skills import mount_dir

LOG_FILE = "evolution.log"
CONFIG_FILE = "ui_config.json"


class Stopped(BaseException):
    """Raised from an episode once a stop is requested. A BaseException, so the loop's
    own ``except Exception`` (which records a failed episode) lets it through."""


@dataclass
class EvolutionConfig:
    name: str
    model: str                          # the agent's OpenRouter model id
    train_tasks: list[str]
    # Who writes the new skills: "self" (the agent's own model: self-evolution, the
    # default) or "teacher" (proposer_model, usually a stronger model).
    proposer: str = "self"
    proposer_model: str = ""
    dev_tasks: list[str] = field(default_factory=list)   # empty: select on the train tasks
    rounds: int = 3
    candidates: int = 3
    reps: int = 1
    workers: int = 4
    patience: int = 0
    method_skill: str = "clm"           # ContextBench's method text: clm | none
    init_skill: str = ""                # a skill library id to start from; "" = no skill
    optimize: str = "reward"            # reward | single_guess
    episode_timeout_s: float = 3600

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvolutionConfig":
        missing = [k for k in ("name", "model", "train_tasks") if not d.get(k)]
        if d.get("proposer") == "teacher" and not d.get("proposer_model"):
            missing.append("teacher model")
        if missing:
            raise ValueError(f"missing: {', '.join(missing)}")
        cfg = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        cfg.name = "".join(c for c in str(cfg.name).strip() if c.isalnum() or c in "-_.")
        if not cfg.name:
            raise ValueError("give the evolution a name (letters, digits, - _ .)")
        cfg.model = str(cfg.model).strip().removeprefix("openrouter/")
        if cfg.proposer not in ("self", "teacher"):
            raise ValueError("proposer must be self or teacher")
        # Self-evolution: the agent's own model proposes its next skills.
        cfg.proposer_model = (cfg.model if cfg.proposer == "self" else
                              str(cfg.proposer_model).strip().removeprefix("openrouter/"))
        if not cfg.train_tasks:
            raise ValueError("choose at least one training task")
        for k in ("rounds", "candidates", "reps", "workers", "patience"):
            setattr(cfg, k, int(getattr(cfg, k)))
        cfg.episode_timeout_s = float(cfg.episode_timeout_s)
        if cfg.method_skill not in ("clm", "none"):
            raise ValueError("method_skill must be clm or none")
        if cfg.optimize not in ("reward", "single_guess"):
            raise ValueError("optimize must be reward or single_guess")
        return cfg

    def n_episodes(self) -> int:
        """Episodes the run needs at most, before caching: the starting skill on the
        development tasks, then per round the incumbent on the training tasks and every
        candidate on the development tasks."""
        dev = len(self.dev_tasks or self.train_tasks)
        return self.reps * (dev + self.rounds * (len(self.train_tasks) + self.candidates * dev))


class ContextBenchSource(TaskSource):
    """ContextBench tasks for ``clm_icl``: one episode = one ``clm_ui.episode`` process."""

    def __init__(self, task_dirs: list[Path], cfg: EvolutionConfig, sandbox: str,
                 pricing: dict[str, Any] | None, job: "EvolutionJob") -> None:
        self.tasks = {d.name: d for d in task_dirs}
        self.cfg, self.sandbox, self.pricing, self.job = cfg, sandbox, pricing or {}, job

    def task_ids(self) -> list[str]:
        return list(self.tasks)

    def describe(self) -> str:
        families = sorted({d.name.split("-seed")[0].rsplit("-", 1)[0] for d in self.tasks.values()})
        method = ("with ContextBench's CLM method text inserted into the task instruction"
                  if self.cfg.method_skill == "clm" else "with no method text")
        return (f"ContextBench tasks ({', '.join(families)}): a long stream of operations "
                f"delivered one at a time to a CLM agent with a 32768-token context budget, "
                f"{method}. The agent manages its context by editing its context file; the "
                f"skill is appended to its system prompt. Score: {self.cfg.optimize}.")

    def run(self, task_id: str, skill_dir: Path | None, out_dir: Path, rep: int) -> Episode:
        if self.job.stop_requested:
            raise Stopped
        name = f"{task_id}__r{rep}"
        cmd = self.command(task_id, skill_dir, out_dir, name)
        log_path = out_dir / f"{name}.log"
        result = run_process(cmd, log_path, self.cfg.episode_timeout_s,
                             self.job.track, self.job.untrack)
        if self.job.stop_requested:
            raise Stopped
        if result is None:
            return Episode(task_id=task_id, rep=rep, reward=None,
                           error=f"episode ended without a result; see {log_path}")
        rewards = result.get("rewards") or {}
        reward = rewards.get(self.cfg.optimize, rewards.get("reward"))
        usage = result.get("usage") or {}
        cost, metric = usage_cost(usage, self.pricing)
        agent = Path(result["run_dir"]) / "agent"
        traj = next((str(p) for p in (agent / "trajectory.ctx.json", agent / "trajectory.json")
                     if p.is_file()), None)
        return Episode(task_id=task_id, rep=rep, reward=reward, cost=cost, cost_metric=metric,
                       trajectory_path=traj, log_dir=result["run_dir"],
                       error=None if reward is not None else "not graded",
                       extra={"rewards": rewards, "n_lm_calls": usage.get("n_lm_calls")})

    def command(self, task_id: str, skill_dir: Path | None, out_dir: Path, name: str) -> list[str]:
        """The episode's process: ``python -m clm_ui.episode`` for one task."""
        cmd = [sys.executable, "-m", "clm_ui.episode", "--task", str(self.tasks[task_id]),
               "--model", self.cfg.model, "--sandbox", self.sandbox, "--out", str(out_dir),
               "--name", name, "--method-skill", self.cfg.method_skill]
        if skill_dir is not None:
            cmd += ["--skill-dir", str(skill_dir)]
        return cmd


class EvolutionJob:
    """``Evolution.run`` in a background thread. One at a time per server."""

    def __init__(self, cfg: EvolutionConfig, run_dir: Path, *, tasks_dir: Path, sandbox: str,
                 skills: Any, pricing: dict[str, Any] | None) -> None:
        self.cfg, self.dir = cfg, run_dir
        self.tasks_dir, self.sandbox, self.skills, self.pricing = tasks_dir, sandbox, skills, pricing
        self.status = "starting"
        self.error: str | None = None
        self.stop_requested = False
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._main, name=f"evolution-{cfg.name}",
                                       daemon=True)

    # -- process tracking, so a stop ends running episodes --------------------------
    def track(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)

    def untrack(self, proc: subprocess.Popen) -> None:
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
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / CONFIG_FILE).write_text(json.dumps(asdict(self.cfg), indent=2) + "\n",
                                            encoding="utf-8")
        self.thread.start()

    def _log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(f"[clm_icl] {line}", flush=True)
        with open(self.dir / LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _tasks(self, names: list[str]) -> list[Path]:
        dirs = [(self.tasks_dir / n).resolve() for n in names]
        for d in dirs:
            if not d.is_relative_to(self.tasks_dir.resolve()) or not (d / "task.toml").is_file():
                raise ValueError(f"no task {d.name!r} in {self.tasks_dir}")
        return dirs

    def _main(self) -> None:
        cfg = self.cfg
        saved_log = _evolve.log
        _evolve.log = self._log   # the loop's progress lines go to evolution.log too
        try:
            self.status = "running"
            train = ContextBenchSource(self._tasks(cfg.train_tasks), cfg, self.sandbox,
                                       self.pricing, self)
            dev = (ContextBenchSource(self._tasks(cfg.dev_tasks), cfg, self.sandbox,
                                      self.pricing, self) if cfg.dev_tasks else None)
            # clm_icl copies the starting skill's folder and reads its SKILL.md; write the
            # text there, whatever the library file is called (cb:<family> is <family>.md).
            init = (mount_dir(self.dir / "init_skill", self.skills.text(cfg.init_skill))
                    if cfg.init_skill else None)
            proposer = litellm_proposer("openrouter/" + cfg.proposer_model)
            who = ("self-evolution" if cfg.proposer == "self"
                   else f"teacher {cfg.proposer_model}")
            self._log(f"evolution {cfg.name}: agent {cfg.model}, {who}, "
                      f"up to {cfg.n_episodes()} episodes")
            evo = Evolution(self.dir, train, proposer, dev=dev, n_candidates=cfg.candidates,
                            reps=cfg.reps, workers=cfg.workers, patience=cfg.patience,
                            init_skill=init)
            best = evo.run(cfg.rounds)
            self.status = "done"
            self._log(f"done: best skill {best}")
        except Stopped:
            self.status = "stopped"
            self._log("stopped; start it again with the same name to resume")
        except BaseException as exc:  # noqa: BLE001 - incl. the loop's SystemExit
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            self._log(f"error: {self.error}\n{traceback.format_exc()}")
        finally:
            _evolve.log = saved_log


# ---------------------------------------------------------------------- reading a run
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_runs(evolutions_dir: Path) -> list[dict[str, Any]]:
    out = []
    if evolutions_dir.is_dir():
        for d in sorted(evolutions_dir.iterdir()):
            state = _read_json(d / "state.json")
            if state is None:
                continue
            out.append({"name": d.name, "round": state.get("round", 0),
                        "incumbent": state.get("incumbent"),
                        "has_best": (d / "best_skill" / "SKILL.md").is_file()})
    return out


def snapshot(run_dir: Path, job: EvolutionJob | None = None) -> dict[str, Any]:
    """Everything the page shows about one run, from its files."""
    state = _read_json(run_dir / "state.json") or {}
    cfg = _read_json(run_dir / CONFIG_FILE) or {}
    skills = []
    for sid, info in (state.get("skills") or {}).items():
        dev = info.get("dev") or {}
        skills.append({"id": sid, "round": info.get("round"), "parent": info.get("parent"),
                       "status": info.get("status"), "gate": info.get("gate"),
                       "acc": dev.get("acc"), "n": dev.get("n"), "n_valid": dev.get("n_valid"),
                       "cost": dev.get("cost"), "cost_metric": dev.get("cost_metric"),
                       "has_text": (run_dir / "skills" / sid / "SKILL.md").is_file()})
    episodes: dict[str, dict[str, int]] = {}
    for f in (run_dir / "episodes").glob("*/*/*.json"):
        split, sid = f.parent.parent.name, f.parent.name
        ep = _read_json(f) or {}
        c = episodes.setdefault(f"{split}/{sid}", {"done": 0, "graded": 0})
        c["done"] += 1
        c["graded"] += ep.get("reward") is not None
    rounds = []
    for r in sorted((run_dir / "rounds").glob("r*"), key=lambda p: int(p.name[1:] or 0)):
        rounds.append({"round": int(r.name[1:]), "gate": _read_json(r / "gate.json"),
                       "has_note": (r / "note.md").is_file(),
                       "has_analysis": (r / "analysis.md").is_file()})
    log_tail = ""
    if (run_dir / LOG_FILE).is_file():
        log_tail = "\n".join((run_dir / LOG_FILE).read_text(encoding="utf-8").splitlines()[-60:])
    status = job.status if job is not None and job.dir == run_dir else (
        "done" if (run_dir / "best_skill").is_dir() else "idle")
    return {"name": run_dir.name, "status": status,
            "error": job.error if job is not None and job.dir == run_dir else None,
            "config": cfg, "round": state.get("round", 0), "incumbent": state.get("incumbent"),
            "frontier": state.get("frontier"), "dev_equals_train": state.get("dev_equals_train"),
            "skills": sorted(skills, key=lambda s: (s["round"] or 0, s["id"])),
            "episodes": episodes, "rounds": rounds,
            "n_episodes_max": (EvolutionConfig.from_dict(cfg).n_episodes() if cfg else None),
            "log": log_tail}


def skill_detail(run_dir: Path, sid: str) -> dict[str, Any]:
    """A skill's text, and its changes from the skill it was proposed to replace."""
    state = _read_json(run_dir / "state.json") or {}
    info = (state.get("skills") or {}).get(sid)
    if info is None:
        raise ValueError(f"no skill {sid!r}")
    text = _skill_text(run_dir, sid)
    parent = info.get("parent")
    parent_text = _skill_text(run_dir, parent) if parent else ""
    return {"id": sid, "text": text, "parent": parent,
            "diff": line_diff(parent_text, text) if parent else None}


def round_file(run_dir: Path, k: int, which: str) -> str:
    if which not in ("note.md", "analysis.md", "proposer_response.txt"):
        raise ValueError(which)
    p = run_dir / "rounds" / f"r{int(k)}" / which
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _skill_text(run_dir: Path, sid: str) -> str:
    p = run_dir / "skills" / sid / "SKILL.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""
