# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""One inspector session: a sandbox, a CLM run, and its event log.

``chat`` runs ``ChatAgent`` and waits for the user's messages. ``task`` runs a
ContextBench task the way ``harbor trials start -a clm_contextbench:ClmStream`` does
(``TaskAgent`` is ``ClmStream``), then grades it with ContextBench's own grader.

Every event is appended to ``events`` and pushed to each connected page, so a page that
reconnects replays the session from the start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harbor.models.agent.context import AgentContext

from . import protocol
from .agent import ChatAgent, TaskAgent
from .sandbox import HarborSandbox, Sandbox, read_host, walk_host

logger = logging.getLogger(__name__)

#: Fields holding a whole context file, by event type, and the group whose newest event
#: alone keeps that field in the log (the page needs only the latest copy).
_HEAVY_FIELDS = {"mirror_write": ("text", "mirror"), "mirror_live": ("text", "mirror"),
                 "ctx_edit": ("text", "mirror"), "context": ("rendered", "context"),
                 "ctx_preview": ("rendered", "context")}

#: How models are named to litellm. The inspector runs OpenRouter models only.
PROVIDER = "openrouter/"
#: Where the agent's commands run: Harbor's Daytona or Docker environment.
SANDBOXES = ("daytona", "docker")


@dataclass
class SessionConfig:
    mode: str = "chat"                 # chat | task
    model: str = "qwen/qwen3.5-9b"     # OpenRouter model id
    task: str | None = None            # task mode: a task directory under the tasks dir
    skill: str = "clm"                 # task mode: clm (the paper's method skill) | none
    # Chat only; a task runs with the paper's settings (clm_contextbench.PAPER_KWARGS).
    context_budget_tokens: int = 32000
    allow_edit_growth: str = "fit"     # fit | shrink
    max_tokens: int | None = None      # None: ClmAgent's default
    temperature: float | None = 0.7

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionConfig":
        cfg = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if cfg.mode not in ("chat", "task"):
            raise ValueError(f"mode must be chat or task, got {cfg.mode!r}. If the page "
                             "offered that mode, it is an old copy: reload it with Ctrl+F5.")
        cfg.model = str(cfg.model or "").strip().removeprefix(PROVIDER)
        if not cfg.model:
            raise ValueError("choose an OpenRouter model")
        if cfg.mode == "task" and not cfg.task:
            raise ValueError("choose a ContextBench task")
        if cfg.skill not in ("clm", "none"):
            raise ValueError(f"skill must be clm or none, got {cfg.skill!r}")
        cfg.context_budget_tokens = int(cfg.context_budget_tokens)
        cfg.max_tokens = int(cfg.max_tokens) if cfg.max_tokens not in ("", None) else None
        cfg.temperature = float(cfg.temperature) if cfg.temperature not in ("", None) else None
        return cfg

    def kwargs(self) -> dict[str, Any]:
        """Agent kwargs. OpenRouter takes no chat_template_kwargs, as for
        ``--agent-kwarg send_chat_template_kwargs=false`` on the Harbor command line."""
        if self.mode == "task":
            # ClmStream adds PAPER_KWARGS; skill=none leaves the method skill out.
            return {"send_chat_template_kwargs": False, "skill": self.skill}
        # ClmAgent's defaults, except: no step or LM-call limit (the user ends the
        # conversation), no final-turn notices, and the task text as is.
        kw: dict[str, Any] = {
            "max_steps": 100000, "lm_call_cap": 0, "finalize_nudge_turns": 0,
            "cost_metric": "usd", "task_template": "open_problems",
            "send_chat_template_kwargs": False,
            "context_budget_tokens": self.context_budget_tokens,
            "allow_edit_growth": self.allow_edit_growth,
            "temperature": self.temperature if self.temperature is not None else "none"}
        if self.max_tokens is not None:
            kw["max_tokens"] = self.max_tokens
        return kw


class Session:
    def __init__(self, cfg: SessionConfig, *, runs_dir: Path, sandbox_kind: str,
                 tasks_dir: Path | None, model_info: dict[str, Any] | None = None) -> None:
        self.cfg = cfg
        self.model_info = model_info or {}
        self.task = None
        if cfg.mode == "task":
            from harbor.models.task.task import Task

            if tasks_dir is None:
                raise ValueError("no tasks directory: start clm-ui with --tasks-dir")
            task_dir = (tasks_dir / cfg.task).resolve()
            if not task_dir.is_relative_to(tasks_dir.resolve()) or not task_dir.is_dir():
                raise ValueError(f"no task {cfg.task!r} in {tasks_dir}")
            self.task = Task(task_dir)
        if sandbox_kind not in SANDBOXES:
            raise ValueError(f"sandbox must be one of {SANDBOXES}, got {sandbox_kind!r}")
        # One folder per run, laid out like a Harbor trial (harbor trials start
        # --trials-dir): <task>__<time>/ with agent/, verifier/, config.json, result.json.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.id = f"{self.task.name if self.task is not None else 'chat'}__{stamp}"
        self.dir = (runs_dir / self.id).resolve()
        self.logs_dir = self.dir / "agent"
        self.verifier_dir = self.dir / "verifier"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.rewards: dict[str, Any] | None = None
        self.sandbox: Sandbox = HarborSandbox(sandbox_kind, self.dir, task=self.task,
                                              name=f"clm-ui-{stamp}")
        self.host_roots = {"agent-logs": self.logs_dir, "verifier": self.verifier_dir}
        self.events: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.agent: ChatAgent | TaskAgent | None = None
        self.run_task: asyncio.Task | None = None
        self.pending: list[str] = []   # chat messages sent before the agent exists
        self.state = "starting"
        self._heavy: dict[str, tuple[dict[str, Any], str]] = {}
        self._fs_lock = asyncio.Lock()

    # ------------------------------------------------------------ events
    def emit(self, type: str, **data: Any) -> None:
        ev = {"seq": len(self.events), "t": time.time(), "type": type, **data}
        field, group = _HEAVY_FIELDS.get(type, (None, None))
        if field and field in ev:
            prev = self._heavy.get(group)
            if prev is not None:
                prev[0].pop(prev[1], None)
            self._heavy[group] = (ev, field)
        if type == "status":
            self.state = data.get("state", self.state)
        self.events.append(ev)
        self._log(ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def _log(self, ev: dict[str, Any]) -> None:
        """Append the event to events.jsonl, the run's log. Whole copies of the context
        file are left out: context_snapshots/ already holds every LM call's input."""
        field, _ = _HEAVY_FIELDS.get(ev["type"], (None, None))
        line = {k: v for k, v in ev.items() if k != field}
        try:
            with open(self.dir / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=str) + "\n")
        except OSError:
            logger.exception("could not write events.jsonl")

    def _write_json(self, name: str, data: dict[str, Any]) -> None:
        (self.dir / name).write_text(json.dumps(data, indent=2, default=str) + "\n",
                                     encoding="utf-8")

    def _write_config(self) -> None:
        """config.json: what this run is, written when it starts."""
        kwargs = self.cfg.kwargs()
        if self.task is not None:
            from clm_contextbench.clm_stream import PAPER_KWARGS
            kwargs = {**PAPER_KWARGS, **kwargs}  # what ClmStream runs with
        self._write_json("config.json", {
            "id": self.id, "started_at": _iso(self.started_at), "mode": self.cfg.mode,
            "model": PROVIDER + self.cfg.model,
            "task": self.task.name if self.task is not None else None,
            "task_dir": str(self.task.task_dir) if self.task is not None else None,
            "sandbox": self.sandbox.kind, "settings": asdict(self.cfg),
            "agent_kwargs": kwargs})

    def _write_result(self, reason: str, usage: Any, terminated_by: Any) -> None:
        """result.json: how the run ended, written when it ends."""
        finished = time.time()
        self._write_json("result.json", {
            "id": self.id, "reason": reason, "terminated_by": terminated_by,
            "rewards": self.rewards, "started_at": _iso(self.started_at),
            "finished_at": _iso(finished), "duration_s": round(finished - self.started_at, 1),
            "usage": usage})

    def describe(self) -> dict[str, Any]:
        sb = self.sandbox
        return {
            "id": self.id, "dir": str(self.dir), "config": asdict(self.cfg),
            "model": PROVIDER + self.cfg.model, "model_info": self.model_info,
            "task": self.task.name if self.task is not None else None,
            "sandbox": sb.kind, "warning": sb.warning, "ctx_file": sb.ctx_file,
            "paths": {"ctx_dir": sb.ctx_dir, "state_dir": sb.state_dir,
                      "workspace": sb.workspace, "logs": str(self.logs_dir)},
        }

    # --------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._write_config()
        self.emit("session", **self.describe())
        self.emit("status", state="starting", detail=f"starting the {self.sandbox.kind} sandbox")
        await self.sandbox.start()
        if self.task is not None:
            # The environment harbor would give the task, untouched: no setup, no probe.
            self.emit("user_message", text=self.task.instruction, kind="task")
            self.run_task = asyncio.create_task(self._run(self.task.instruction))
        else:
            await self._prepare_chat_sandbox()
        await self.refresh_fs()

    async def _prepare_chat_sandbox(self) -> None:
        task = protocol.CHAT_TASK
        self.emit("status", state="starting", detail="installing curl in the sandbox")
        installed = await self.sandbox.install_tools()
        if installed:
            self.emit("notice", text=f"Sandbox setup: {installed}.")
        self.emit("status", state="starting", detail="checking the sandbox's internet access")
        probe = await self.sandbox.probe_network()
        if probe:
            web_ok = any(v.startswith("ok") for k, v in probe.items() if "pypi" not in k)
            self.emit("network", hosts=probe, web=web_ok)
            if not web_ok:
                task += " " + protocol.NO_WEB_NOTE
        self.run_task = asyncio.create_task(self._run(task))

    async def send(self, text: str) -> None:    # user message in chat vs task by bench
        text = text.strip()
        if not text:
            return
        if self.task is not None:
            raise ValueError("a ContextBench task feeds its own ops; there is nothing to send")
        busy = isinstance(self.agent, ChatAgent) and not self.agent.waiting
        self.emit("user_message", text=text, kind="turn", queued=busy)
        if self.agent is None:
            self.pending.append(text)
        else:
            self.agent.inbox.put_nowait(text)

    async def _run(self, instruction: str) -> None: # run agent in background
        cfg, sb = self.cfg, self.sandbox
        reason = "ended"
        try:
            common = dict(logs_dir=self.logs_dir, model_name=PROVIDER + cfg.model,
                          emit=self.emit, sandbox=sb, refresh_fs=self.refresh_fs,
                          **cfg.kwargs())
            agent: ChatAgent | TaskAgent = (
                TaskAgent(**common) if self.task is not None else ChatAgent(**common))
            self.agent = agent
            if isinstance(agent, ChatAgent):
                for text in self.pending:
                    agent.inbox.put_nowait(text)
                self.pending.clear()
            await agent.setup(sb)
            await agent.run(instruction, sb, AgentContext())
            reason = "completed"
        except asyncio.CancelledError:
            reason = "stopped"
        except Exception as exc:  # shown on the page, not swallowed
            reason = "error"
            logger.exception("session %s failed", self.id)
            self.emit("error", message=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc())
        finally:
            usage = _read_json(self.logs_dir / "usage.json")    
            stream = _read_json(self.logs_dir / "interactive_stream.json")  # 
            terminated_by = (stream or {}).get("terminated_by")
            self.emit("finished", reason=reason, usage=usage, terminated_by=terminated_by)
            if self.task is not None and reason in ("completed", "error"):
                await self._grade()
            self._write_result(reason, usage, terminated_by)
            self.emit("status", state="finished", detail=reason)
            try:
                await self.refresh_fs()
            except Exception:  # noqa: BLE001 - the sandbox may already be gone
                pass

    async def _grade(self) -> None:
        """ContextBench's own grader, as the task's tests/test.sh runs it, on the host."""
        from contextbench.harbor.reward import write_rewards

        gold = Path(self.task.task_dir) / "tests" / "gold.json"
        self.emit("status", state="grading", detail="grading with ContextBench")
        try:
            numeric = await asyncio.to_thread(write_rewards, self.logs_dir, gold,
                                              self.verifier_dir)
            self.rewards = numeric
            self.emit("grade", rewards=numeric)
        except Exception as exc:  # noqa: BLE001 - a failed grade is reported, not fatal
            logger.exception("grading failed")
            self.emit("error", message=f"grading failed: {type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc())

    async def stop(self) -> None:
        if self.run_task and not self.run_task.done():
            self.run_task.cancel()
            try:
                await self.run_task
            except BaseException:  # noqa: BLE001
                pass
        await self.sandbox.stop()

    # -------------------------------------------------------- file system
    async def refresh_fs(self) -> None:
        async with self._fs_lock:
            listed = await self.sandbox.list_trees([r["path"] for r in self.sandbox.roots()])
            roots = [{**r, "where": "sandbox", "entries": listed[r["path"]]}
                     for r in self.sandbox.roots()]
            roots.append({
                "path": "agent-logs", "label": "agent logs (host)", "where": "host",
                "about": "The agent's outputs. context_snapshots/turn-NNNN.json is each LM "
                         "call's exact input (what ContextBench grades); trajectory.json, "
                         "usage.json and timing.json are written when the run ends.",
                "entries": walk_host(self.logs_dir, "agent-logs")})
            if self.task is not None:
                roots.append({
                    "path": "verifier", "label": "grading (host)", "where": "host",
                    "about": "ContextBench's grader output, written after the run.",
                    "entries": walk_host(self.verifier_dir, "verifier")})
            self.emit("fs", roots=roots)

    async def read_file(self, where: str, path: str) -> str | None:
        if ".." in path.split("/"):
            return None
        if where == "host":
            head, _, rest = path.partition("/")
            base = self.host_roots.get(head)
            if base is None:
                return None
            target = (base / rest).resolve()
            return read_host(target) if target.is_relative_to(base.resolve()) else None
        if not any(path == r["path"] or path.startswith(r["path"] + "/")
                   for r in self.sandbox.roots()):
            return None
        return await self.sandbox.read_text(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
