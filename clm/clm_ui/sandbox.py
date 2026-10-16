# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Sandboxes for the inspector: Daytona or Docker, through Harbor's own environments.

The agents call only ``exec``, ``upload_file`` and ``download_file`` on their sandbox;
the file-system column also needs ``list_trees`` and ``read_text``. ``HarborSandbox``
wraps Harbor's Daytona or Docker environment, so the context file sits at the paper's
path, ``/tmp/.live_ctx/LIVE_CTX_MAIN.txt``, and a ContextBench task gets the environment
``harbor trials start`` would give it.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from harbor.environments.base import ExecResult

from clm_harness.context_env import paths as default_paths

#: Entries returned per root by ``list_tree``; a workspace with a cloned repo stays usable.
MAX_TREE_ENTRIES = 400
MAX_TREE_DEPTH = 5
#: Bytes of a file the viewer fetches.
MAX_READ_BYTES = 2_000_000
#: find -printf fields after the root: type, size, mtime, path (find expands the escapes).
_FIND_FORMAT = r"\t%y\t%s\t%T@\t%p\n"

#: python:3.12-slim has no curl or wget. apt registries are reachable from every
#: Daytona tier, so this works even where the open internet is blocked.
_INSTALL_CURL = (
    "command -v curl >/dev/null 2>&1 && exit 0; "
    "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null && "
    "apt-get install -y -qq --no-install-recommends curl wget ca-certificates >/dev/null")

#: Hosts whose reachability tells an open network from an allowlisted one: PyPI is an
#: "essential service" on every Daytona tier; the others are ordinary web hosts.
_PROBE_HOSTS = ("https://pypi.org/simple/", "https://example.com/",
                "https://export.arxiv.org/api/query?search_query=all:test&max_results=1")
_PROBE = """python3 - <<'PY'
import json, urllib.request
out = {}
for url in %s:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl/8"}), timeout=8) as r:
            out[url] = f"ok {r.status}"
    except Exception as e:
        out[url] = f"blocked: {type(e).__name__}: {str(e)[:80]}"
print(json.dumps(out))
PY""" % (list(_PROBE_HOSTS),)


def _entry(path: str, kind: str, size: int, mtime: float) -> dict[str, Any]:
    return {"path": path, "kind": kind, "size": size, "mtime": mtime}


class Sandbox:
    """Common interface. ``ctx_dir`` / ``state_dir`` are the harness's mirror and
    persistent-shell paths as seen from inside the sandbox."""

    kind = "base"
    ctx_dir: str = default_paths.CTX_DIR
    state_dir: str = default_paths.STATE_DIR
    workspace: str = "/workspace"
    warning: str | None = None
    #: Seconds between reads of the context file while a command runs.
    live_poll_s: float = 0.4

    @property
    def ctx_file(self) -> str:
        return f"{self.ctx_dir}/LIVE_CTX_MAIN.txt"

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def exec(self, command: str, timeout_sec: int | None = None, **_: Any) -> ExecResult:
        raise NotImplementedError

    async def upload_file(self, source_path: str | Path, target_path: str) -> None:
        raise NotImplementedError

    async def download_file(self, source_path: str, target_path: str | Path) -> None:
        raise NotImplementedError

    async def list_tree(self, root: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def read_text(self, path: str) -> str | None:
        raise NotImplementedError

    async def list_trees(self, roots: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {r: await self.list_tree(r) for r in roots}

    async def install_tools(self) -> str | None:
        """Install the command-line tools a chat task expects; None if nothing to do."""
        return None

    async def probe_network(self) -> dict[str, str] | None:
        """Which hosts the sandbox can reach; None if not checked."""
        return None

    def roots(self) -> list[dict[str, str]]:
        """The directories the file-system column shows, with what each one is for."""
        return [
            {"path": self.ctx_dir, "label": "context mirror",
             "about": "The model's editable context, rewritten before every command."},
            {"path": self.state_dir, "label": "persistent shell",
             "about": "cwd and exported env, restored before each command."},
            {"path": self.workspace, "label": "workspace",
             "about": "The shell's starting directory, where task files go."},
        ]


def _run_sync(argv: list[str], *, timeout: float | None, cwd: str | None = None,
              env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult:
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout,
                              cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace")
        err = (exc.stderr or b"").decode("utf-8", "replace")
        return ExecResult(stdout=out, stderr=err + f"\n[timed out after {timeout}s]",
                          return_code=124)
    return ExecResult(stdout=proc.stdout.decode("utf-8", "replace"),
                      stderr=proc.stderr.decode("utf-8", "replace"),
                      return_code=proc.returncode)


async def _run(argv: list[str], **kw: Any) -> ExecResult:
    # A thread, not asyncio subprocesses: those need the Proactor loop on Windows,
    # which the server's loop may not be.
    return await asyncio.to_thread(_run_sync, argv, **kw)


def walk_host(base: Path, shown_as: str) -> list[dict[str, Any]]:
    """Entries under a host directory, with paths rewritten to start at ``shown_as``."""
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        if len(Path(dirpath).relative_to(base).parts) >= MAX_TREE_DEPTH:
            dirnames[:] = []
        dirnames.sort()
        for name in dirnames + sorted(filenames):
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(base).as_posix()
            out.append(_entry(f"{shown_as}/{rel}", "d" if p.is_dir() else "f",
                              st.st_size, st.st_mtime))
            if len(out) >= MAX_TREE_ENTRIES:
                return out
    return out


def read_host(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_READ_BYTES).decode("utf-8", "replace")
    except OSError:
        return None


# ---------------------------------------------------------------- docker
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    # Only the exit code matters. Without pipes the timeout holds: on Windows, captured
    # output keeps the call waiting for docker's helper processes after the kill.
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


class ShellSandbox(Sandbox):
    """A remote sandbox whose files are reached only through a shell (``_sh``)."""

    async def _sh(self, command: str, timeout: float | None) -> ExecResult:
        raise NotImplementedError

    async def list_trees(self, roots: list[str]) -> dict[str, list[dict[str, Any]]]:
        # One call for every root: each call is a round trip to the sandbox. find's
        # -printf expands \t and \n itself; each line starts with its root.
        out: dict[str, list[dict[str, Any]]] = {r: [] for r in roots}
        script = "\n".join(
            f"find {shlex.quote(r)} -mindepth 1 -maxdepth {MAX_TREE_DEPTH} "
            f"-printf {shlex.quote(r + _FIND_FORMAT)} 2>/dev/null | sort | head -n {MAX_TREE_ENTRIES}"
            for r in roots)
        res = await self._sh(script, timeout=60)
        for line in res.stdout.splitlines():
            parts = line.split("\t", 4)
            if len(parts) == 5 and parts[0] in out:
                root, kind, size, mtime, path = parts
                out[root].append(_entry(path, "d" if kind == "d" else "f", int(size),
                                        float(mtime)))
        for entries in out.values():
            entries.sort(key=lambda e: e["path"])
        return out

    async def list_tree(self, root: str) -> list[dict[str, Any]]:
        return (await self.list_trees([root]))[root]

    async def read_text(self, path: str) -> str | None:
        res = await self._sh(f"head -c {MAX_READ_BYTES} {shlex.quote(path)}", timeout=30)
        return res.stdout if res.return_code == 0 else None


# ---------------------------------------------------------------- harbor
#: The environment a chat session gets: what a ContextBench task's [environment] asks for.
CHAT_ENV = {"docker_image": "python:3.12-slim", "workdir": "/workspace", "cpus": 1,
            "memory_mb": 2048, "storage_mb": 4096}


class HarborSandbox(ShellSandbox):
    """A Daytona or Docker sandbox, through Harbor's own environment classes.

    Built from a ContextBench task, it is the environment ``harbor trials start -e <kind>``
    gives that task: the task's image, workdir and resources, with its ``environment/``
    directory (the op stream) uploaded into the workdir. For chat, an empty environment
    directory and ``CHAT_ENV``. Credentials are Harbor's: ``DAYTONA_API_KEY`` (or
    ``DAYTONA_JWT_TOKEN`` and ``DAYTONA_ORGANIZATION_ID``) for Daytona.
    """

    def __init__(self, kind: str, session_dir: Path, *, task: Any = None,
                 image: str | None = None, name: str | None = None) -> None:
        from harbor.environments.factory import EnvironmentFactory
        from harbor.models.environment_type import EnvironmentType
        from harbor.models.task.config import EnvironmentConfig
        from harbor.models.trial.paths import TrialPaths

        self.kind = kind
        self.live_poll_s = 1.5 if kind == "daytona" else 0.4  # each read is a remote exec
        if task is not None:
            env_dir, config = Path(task.paths.environment_dir), task.config.environment
        else:
            env_dir = session_dir / "harbor" / "environment"  # empty: nothing to upload
            env_dir.mkdir(parents=True, exist_ok=True)
            config = EnvironmentConfig(**{**CHAT_ENV, **({"docker_image": image} if image else {})})
        self.workspace = config.workdir or "/workspace"
        self.env = EnvironmentFactory.create_environment(
            type=EnvironmentType(kind), environment_dir=env_dir,
            environment_name=task.name if task is not None else "clm-ui",
            session_id=name or f"clm-ui-{uuid.uuid4().hex[:8]}",
            trial_paths=TrialPaths(session_dir / "harbor" / "trial"),
            task_env_config=config)

    @property
    def task_env_config(self) -> Any:
        # ContextBench reads the stream's path, op count and sha from here.
        return self.env.task_env_config

    def preflight(self) -> None:
        """Raise if the backend is unusable. Harbor's own check exits the process,
        which would take the server down with it."""
        try:
            type(self.env).preflight()
        except SystemExit as exc:
            raise RuntimeError(str(exc) or f"{self.kind} is not available") from None

    async def start(self) -> None:
        self.preflight()
        await self.env.start(force_build=False)

    async def stop(self) -> None:
        await self.env.stop(delete=True)

    async def _sh(self, command: str, timeout: float | None) -> ExecResult:
        return await self.env.exec(command=command, timeout_sec=int(timeout) if timeout else None)

    async def exec(self, command: str, timeout_sec: int | None = None, **_: Any) -> ExecResult:
        return await self.env.exec(command=command, cwd=self.workspace, timeout_sec=timeout_sec)

    async def upload_file(self, source_path: str | Path, target_path: str) -> None:
        await self.env.upload_file(source_path, target_path)

    async def download_file(self, source_path: str, target_path: str | Path) -> None:
        await self.env.download_file(source_path, target_path)

    async def install_tools(self) -> str | None:
        res = await self.env.exec(command=_INSTALL_CURL, timeout_sec=300)
        if res.return_code != 0:
            return f"could not install curl: {(res.stderr or res.stdout or '').strip()[:200]}"
        return "curl and wget are installed"

    async def probe_network(self) -> dict[str, str] | None:
        import json

        res = await self.env.exec(command=_PROBE, timeout_sec=60)
        try:
            return json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"probe": f"failed: {(res.stderr or res.stdout).strip()[:200]}"}
