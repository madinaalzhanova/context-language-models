# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Run one ContextBench task once, without the page, and print its result.

    python -m clm_ui.episode --task cb_tasks/<task> --model qwen/qwen3.5-9b \\
        --out <dir> --name <task>__r0 [--skill-dir <dir with SKILL.md>] [--agent baseline]

It is a ``Session`` in task mode, as the page runs it: Harbor's Daytona or Docker
environment for the task, ``ClmStream`` with the paper's settings, the skill (if any)
appended to the system prompt through ``skill_dirs``, and ContextBench's grader plus
``single_guess``. ``--agent baseline`` runs the base model without CLM instead
(ContextBench's append-only agent). The run folder is ``<out>/<name>/``. The last line
printed is ``EPISODE_RESULT {json}``.

The evolution loop (``clm_ui.evolution``) and the benchmark (``clm_ui.benchmark``) run
each episode this way, in its own process, so episodes run side by side and can be
stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .session import AGENTS, METHOD_SKILLS, Session, SessionConfig

RESULT_PREFIX = "EPISODE_RESULT "
#: Seconds a stopped episode gets to delete its sandbox and exit before it is killed.
STOP_GRACE_S = 90.0
#: An episode in its own process group, so it can be sent a stop signal on its own
#: (Windows: CTRL_BREAK_EVENT reaches a whole process group).
_POPEN_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


def stop_process(proc: subprocess.Popen, grace_s: float = STOP_GRACE_S) -> None:
    """Ask an episode process to stop (it cancels its run and deletes its sandbox), wait
    up to ``grace_s`` seconds, then kill it. Killing it outright would leave its sandbox
    running."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        proc.wait(timeout=grace_s)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        proc.kill()


def run_process(cmd: list[str], log_path: Path, timeout_s: float,
                track: Any = None, untrack: Any = None) -> dict | None:
    """Run one episode process; its stderr and stdout go to ``log_path``. Returns the
    ``EPISODE_RESULT`` it printed, or None (crashed, killed, or timed out). ``track`` and
    ``untrack`` get the process, so a caller can terminate it to stop the episode."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True,
                                encoding="utf-8", env=dict(os.environ),
                                creationflags=_POPEN_FLAGS)
        if track:
            track(proc)
        try:
            stdout, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            stop_process(proc)
            stdout = ""
        finally:
            if untrack:
                untrack(proc)
        log.write(stdout)
    line = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith(RESULT_PREFIX)),
                None)
    return json.loads(line[len(RESULT_PREFIX):]) if line is not None else None


def usage_cost(usage: dict, pricing: dict | None) -> tuple[float | None, str | None]:
    """An episode's cost: USD at OpenRouter's list prices, or tokens when the model has
    no price."""
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if pt is None:
        return None, None
    p_in, p_out = (pricing or {}).get("prompt"), (pricing or {}).get("completion")
    if p_in is not None and p_out is not None:
        return float(pt) * float(p_in) + float(ct or 0) * float(p_out), "usd"
    return float(pt) + float(ct or 0), "tokens"


async def run_episode(task_dir: Path, *, model: str, sandbox: str, out: Path, name: str,
                      skill_dir: Path | None, method_skill: str, agent: str = "clm") -> dict:
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8") if skill_dir else ""
    cfg = SessionConfig.from_dict({
        "mode": "task", "task": task_dir.name, "model": model, "skill": method_skill,
        "agent": agent,
        "steering": "custom" if text else "", "steering_text": text})
    s = Session(cfg, runs_dir=out, sandbox_kind=sandbox, tasks_dir=task_dir.parent,
                run_name=name)
    try:
        await s.start()
        if s.run_task is not None:
            await s.run_task
    finally:
        await s.stop()
    usage = json.loads((s.logs_dir / "usage.json").read_text()) \
        if (s.logs_dir / "usage.json").is_file() else {}
    return {"run_dir": str(s.dir), "rewards": s.rewards, "usage": usage}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m clm_ui.episode", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True, type=Path, help="a packaged ContextBench task")
    ap.add_argument("--model", required=True, help="OpenRouter model id")
    ap.add_argument("--sandbox", choices=("daytona", "docker"), default="daytona")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--name", required=True, help="the run folder's name under --out")
    ap.add_argument("--skill-dir", type=Path, default=None, help="a folder with a SKILL.md")
    ap.add_argument("--method-skill", choices=METHOD_SKILLS, default="clm")
    ap.add_argument("--agent", choices=AGENTS, default="clm",
                    help="clm (ClmStream) or baseline (the base model without CLM)")
    a = ap.parse_args(argv)
    try:
        result = asyncio.run(_stoppable(run_episode(
            a.task.resolve(), model=a.model, sandbox=a.sandbox, out=a.out.resolve(),
            name=a.name, skill_dir=a.skill_dir.resolve() if a.skill_dir else None,
            method_skill=a.method_skill, agent=a.agent)))
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("stopped; the sandbox was deleted", file=sys.stderr, flush=True)
        return 1
    print(RESULT_PREFIX + json.dumps(result, default=str), flush=True)
    return 0


async def _stoppable(coro: Any) -> Any:
    """Run ``coro``; a stop signal (SIGTERM, or CTRL_BREAK_EVENT on Windows) cancels it,
    so ``run_episode`` stops the session and deletes the sandbox before the process
    exits."""
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(task.cancel))
    return await task


if __name__ == "__main__":
    sys.exit(main())
