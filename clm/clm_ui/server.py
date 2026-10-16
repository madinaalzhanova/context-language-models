# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""``clm-ui``: serve the inspector page and run one CLM session at a time.

    clm-ui                                             # Daytona, the default
    clm-ui --model qwen/qwen3.6-27b --sandbox docker

Models are OpenRouter models (``OPENROUTER_API_KEY``); the page lists those that support
tool calls and changes the model per session. A session is a chat, or a ContextBench task
from ``--tasks-dir`` run as ``harbor trials start -a clm_contextbench:ClmStream`` runs it.

The page talks to ``/ws``. It sends ``new_session`` (with a config), ``send`` (a chat
message) and ``stop``; it receives the session's events, replayed from the start on
connect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import tomllib
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .sandbox import docker_available
from .session import Session, SessionConfig

STATIC = Path(__file__).resolve().parent / "static"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
#: Re-fetch OpenRouter's model list after this many seconds.
MODELS_TTL_S = 3600


class Hub:
    """The current session, the settings new sessions start from, and the catalogs."""

    def __init__(self, defaults: SessionConfig, runs_dir: Path, sandbox: str,
                 tasks_dir: Path | None) -> None:
        self.defaults = defaults
        self.runs_dir = runs_dir
        self.sandbox = sandbox
        self.tasks_dir = tasks_dir
        self.session: Session | None = None
        self.lock = asyncio.Lock()
        self._models: tuple[float, list[dict[str, Any]]] | None = None

    async def models(self) -> list[dict[str, Any]]:
        """OpenRouter models that take tools (CLM acts only through the bash tool)."""
        if self._models and time.time() - self._models[0] < MODELS_TTL_S:
            return self._models[1]

        def fetch() -> list[dict[str, Any]]:
            with urllib.request.urlopen(OPENROUTER_MODELS, timeout=20) as r:
                data = json.load(r)["data"]
            return sorted(
                ({"id": m["id"], "name": m.get("name", m["id"]),
                  "context_length": m.get("context_length"), "pricing": m.get("pricing", {})}
                 for m in data if "tools" in (m.get("supported_parameters") or [])),
                key=lambda m: m["id"])

        models = await asyncio.to_thread(fetch)
        self._models = (time.time(), models)
        return models

    def tasks(self) -> list[dict[str, Any]]:
        """Packaged ContextBench tasks: directories with an instruction and a task.toml."""
        if self.tasks_dir is None or not self.tasks_dir.is_dir():
            return []
        out = []
        for d in sorted(self.tasks_dir.iterdir()):
            toml = d / "task.toml"
            if not (toml.is_file() and (d / "instruction.md").is_file()):
                continue
            try:
                meta = tomllib.loads(toml.read_text(encoding="utf-8")).get("metadata", {})
            except (OSError, tomllib.TOMLDecodeError):
                meta = {}
            out.append({"name": d.name, "family": meta.get("contextbench_family"),
                        "ops": meta.get("contextbench_stream_count"),
                        "regime": meta.get("contextbench_regime"),
                        "input_tokens": meta.get("instance_approx_input_tokens")})
        return out

    async def new_session(self, cfg: SessionConfig) -> Session:
        async with self.lock:
            if self.session is not None:
                await self.session.stop()
            info = None
            try:
                info = next((m for m in await self.models() if m["id"] == cfg.model), None)
            except Exception:  # noqa: BLE001 - the catalog is optional (pricing display)
                pass
            self.session = Session(cfg, runs_dir=self.runs_dir, sandbox_kind=self.sandbox,
                                   tasks_dir=self.tasks_dir, model_info=info)
            await self.session.start()
            return self.session


def create_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if hub.session is not None:
            await hub.session.stop()

    app = FastAPI(title="CLM Inspector", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    subscribers: set[asyncio.Queue] = set()

    @app.middleware("http")
    async def no_cache(request: Any, call_next: Any) -> Any:
        # The page and its scripts change with the code; a cached app.js would show
        # a restarted server's events with the old page logic.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/defaults")
    async def defaults() -> dict[str, Any]:
        return {"config": asdict(hub.defaults), "sandbox": hub.sandbox,
                "tasks_dir": str(hub.tasks_dir) if hub.tasks_dir else None}

    @app.get("/api/models")
    async def models() -> JSONResponse:
        try:
            return JSONResponse({"models": await hub.models()})
        except Exception as exc:  # noqa: BLE001 - the page falls back to free text
            return JSONResponse({"models": [], "error": f"{type(exc).__name__}: {exc}"})

    @app.get("/api/tasks")
    async def tasks() -> dict[str, Any]:
        return {"tasks": hub.tasks()}

    @app.get("/api/file")
    async def read_file(where: str, path: str) -> JSONResponse:
        s = hub.session
        text = await s.read_file(where, path) if s is not None else None
        if text is None:
            return JSONResponse({"error": "not readable"}, status_code=404)
        return JSONResponse({"path": path, "text": text})

    def attach(q: asyncio.Queue) -> None:
        """Replay the current session into ``q`` and subscribe it."""
        s = hub.session
        if s is None:
            q.put_nowait({"type": "no_session"})
            return
        q.put_nowait({"type": "reset"})
        for ev in s.events:
            q.put_nowait(ev)
        s.subscribers.add(q)

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        subscribers.add(q)
        attach(q)

        async def pump() -> None:
            while True:
                await socket.send_json(await q.get())

        sender = asyncio.create_task(pump())
        try:
            while True:
                msg = await socket.receive_json()
                kind = msg.get("type")
                try:
                    if kind == "new_session":
                        cfg = SessionConfig.from_dict({**asdict(hub.defaults),
                                                       **(msg.get("config") or {})})
                        if hub.session is not None:
                            for sub in subscribers:
                                hub.session.subscribers.discard(sub)
                        s = await hub.new_session(cfg)
                        for sub in subscribers:
                            sub.put_nowait({"type": "reset"})
                            for ev in s.events:
                                sub.put_nowait(ev)
                            s.subscribers.add(sub)
                    elif kind == "send" and hub.session is not None:
                        await hub.session.send(str(msg.get("text", "")))
                    elif kind == "stop" and hub.session is not None:
                        await hub.session.stop()
                except Exception as exc:  # report to the page, keep the socket
                    logging.exception("ws %s failed", kind)
                    q.put_nowait({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            subscribers.discard(q)
            if hub.session is not None:
                hub.session.subscribers.discard(q)

    return app


def _default_tasks_dir() -> Path | None:
    """``cb_tasks`` of the ContextBench checkout that ``contextbench`` is imported from."""
    try:
        import contextbench
    except ImportError:
        return None
    d = Path(contextbench.__file__).resolve().parents[1] / "cb_tasks"
    return d if d.is_dir() else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="clm-ui", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=os.environ.get("CLM_UI_MODEL", "qwen/qwen3.5-9b"),
                    help="OpenRouter model id the dialog starts with (default: qwen/qwen3.5-9b)")
    ap.add_argument("--sandbox", choices=("daytona", "docker"), default="daytona",
                    help="where the model's commands run (default: daytona)")
    ap.add_argument("--tasks-dir", type=Path, default=None,
                    help="packaged ContextBench tasks (default: cb_tasks of the ContextBench "
                         "checkout)")
    ap.add_argument("--env-file", help="a .env file to load (API keys, Daytona credentials)")
    ap.add_argument("--budget", type=int, default=32000,
                    help="chat's context_budget_tokens (tasks use the paper's 32768)")
    ap.add_argument("--runs-dir", default="tests",
                    help="one folder per run, laid out like a Harbor trial (default: tests)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args(argv)

    if args.env_file:
        from dotenv import load_dotenv
        if not load_dotenv(args.env_file, override=True):
            sys.exit(f"clm-ui: could not read {args.env_file}")
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("clm-ui: set OPENROUTER_API_KEY (or pass --env-file).")

    sandbox = args.sandbox
    if sandbox == "daytona" and not (os.environ.get("DAYTONA_API_KEY") or (
            os.environ.get("DAYTONA_JWT_TOKEN") and os.environ.get("DAYTONA_ORGANIZATION_ID"))):
        sys.exit("clm-ui: Daytona needs DAYTONA_API_KEY (or DAYTONA_JWT_TOKEN and "
                 "DAYTONA_ORGANIZATION_ID), or pass --env-file. For Docker: --sandbox docker.")
    if sandbox == "docker" and not docker_available():
        sys.exit("clm-ui: Docker is not running. Start Docker Desktop, or use Daytona.")
    tasks_dir = (args.tasks_dir or _default_tasks_dir())
    defaults = SessionConfig(model=args.model.removeprefix("openrouter/"),
                             context_budget_tokens=args.budget)
    hub = Hub(defaults, Path(args.runs_dir).resolve(), sandbox,
              tasks_dir.resolve() if tasks_dir else None)

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"CLM Inspector on http://{args.host}:{args.port}  (sandbox: {sandbox}, "
          f"tasks: {hub.tasks_dir or 'none'})")
    uvicorn.run(create_app(hub), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
