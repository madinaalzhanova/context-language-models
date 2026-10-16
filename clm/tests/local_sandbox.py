# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Test helper: a sandbox that runs bash on this machine, for test_clm_ui.py.

clm_ui runs only on Daytona or Docker; the end-to-end tests stand this in for them so
they need neither. The commands the tests run are scripted and harmless.

On Windows, /tmp means one folder to Git Bash and another to a Windows Python, so the
context file and shell state go under the sandbox folder instead, and harness_paths()
points the harness (and the path written in its system prompt) there.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from harbor.environments.base import ExecResult

from clm_harness.clm_agent import harness
from clm_ui.sandbox import Sandbox, _run, read_host, walk_host


@contextlib.contextmanager
def harness_paths(ctx_dir: str, state_dir: str):
    """Point the harness at another context-file folder for the duration of the block.

    clm_harness.context_env.paths reads these harness module globals late for this
    purpose. The system prompt names the path literally, so it is rewritten too.
    """
    default_dir = harness._CTX_DIR
    if ctx_dir == default_dir and state_dir == harness._STATE_DIR:
        yield
        return
    saved = (harness._CTX_DIR, harness._CTX_FILE, harness._STATE_DIR, harness._SYSTEM_TEMPLATE)
    harness._CTX_DIR = ctx_dir
    harness._CTX_FILE = f"{ctx_dir}/LIVE_CTX_MAIN.txt"
    harness._STATE_DIR = state_dir
    harness._SYSTEM_TEMPLATE = saved[3].replace(default_dir, ctx_dir)
    try:
        yield
    finally:
        (harness._CTX_DIR, harness._CTX_FILE, harness._STATE_DIR,
         harness._SYSTEM_TEMPLATE) = saved


def find_bash() -> str | None:
    """bash, or None. On Windows this must be Git Bash, not WSL's System32\\bash.exe."""
    found = shutil.which("bash")
    if found and not (sys.platform == "win32" and "system32" in found.lower()):
        return found
    if sys.platform == "win32" and shutil.which("git"):
        root = Path(shutil.which("git")).resolve().parents[1]
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.exists():
                return str(cand)
    return None


class LocalSandbox(Sandbox):
    """bash on this machine, under ``root``. A ContextBench task's environment/ is copied
    into ``root/app`` (where Harbor would upload it), with the stream path pointed there."""

    kind = "local"

    def __init__(self, root: Path, task: Any = None) -> None:
        self.root = root.resolve()
        self.bash = find_bash()
        self.windows = sys.platform == "win32"
        ws = self.root / ("app" if task is not None else "workspace")
        ws.mkdir(parents=True, exist_ok=True)
        self.task_env_config = None
        if task is not None:
            shutil.copytree(task.paths.environment_dir, ws, dirs_exist_ok=True)
            cfg = task.config.environment
            workdir = cfg.workdir or "/app"
            env = {k: (ws.as_posix() + v[len(workdir):] if k == "CONTEXTBENCH_STREAM_PATH"
                       and v.startswith(workdir) else v) for k, v in (cfg.env or {}).items()}
            self.task_env_config = cfg.model_copy(update={"env": env, "workdir": ws.as_posix()})
        if self.windows:
            base = self.root.as_posix()
            self.ctx_dir = f"{base}/tmp/.live_ctx"
            self.state_dir = f"{base}/tmp/.bash_ctx_state"
            self.workspace = ws.as_posix()
        else:
            self.workspace = str(ws)
        self.bin_dir = self.root / "bin"

    async def start(self) -> None:
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        if self.windows:
            # On Windows python3 is usually the Store alias; point it at this interpreter.
            (self.bin_dir / "python3").write_text(
                f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
                encoding="utf-8")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + (";" if self.windows else os.pathsep) + env["PATH"]
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    async def exec(self, command: str, timeout_sec: int | None = None, **_: Any) -> ExecResult:
        # From a script file: a multi-line command passed through argv loses its quotes
        # on Windows.
        fd, script = tempfile.mkstemp(prefix="cmd_", suffix=".sh", dir=self.root)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(command)
        try:
            return await _run([self.bash, Path(script).as_posix()], timeout=timeout_sec,
                              cwd=self.workspace, env=self._env())
        finally:
            Path(script).unlink(missing_ok=True)

    async def upload_file(self, source_path: str | Path, target_path: str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)

    async def download_file(self, source_path: str, target_path: str | Path) -> None:
        shutil.copyfile(source_path, target_path)

    async def list_tree(self, root: str) -> list[dict[str, Any]]:
        return walk_host(Path(root), root)

    async def read_text(self, path: str) -> str | None:
        return read_host(Path(path))
