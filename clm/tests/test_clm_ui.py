# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""CLM Inspector (clm_ui): helpers, a chat session, and a ContextBench task session.

The session tests run the real loops (``ClmAgent``; ``ClmStream`` with ContextBench's
stream loading, delivery and grader). Two things are replaced, so no API key, Daytona or
Docker is needed: the OpenRouter call, by scripted replies, and the sandbox, by
clm_ui_me's LocalSandbox (bash on this machine; the scripted commands are harmless).
They need harbor, litellm, contextbench and bash, and the task test a ContextBench
checkout with cb_tasks.

    PYTHONPATH=clm python -m unittest clm.tests.test_clm_ui -v
"""

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clm_ui_me"))

try:
    import litellm

    from clm_harness.clm_agent.harness import ClmAgent
    from clm_ui import protocol
    from clm_ui.agent import line_diff
    from clm_ui.server import _default_tasks_dir
    from clm_ui.session import Session, SessionConfig
    from local_sandbox import LocalSandbox, find_bash, harness_paths
except ImportError as exc:  # harbor / litellm / contextbench not installed
    raise unittest.SkipTest(f"clm_ui dependencies missing: {exc}")


def _apply(old: str, new: str, ops: list[dict]) -> list[str]:
    """Rebuild ``new`` the way the page does: eq runs from the new text."""
    lines, out, j = new.splitlines(), [], 0
    for op in ops:
        if op["op"] == "eq":
            out += lines[j:j + op["n"]]
            j += op["n"]
        elif op["op"] == "add":
            out += op["lines"]
            j += len(op["lines"])
    return out


def _reply(content: str = "", command: str | None = None) -> "litellm.ModelResponse":
    """A model turn: a bash call when ``command`` is given, else plain text."""
    message = {"role": "assistant", "content": content}
    if command is not None:
        message["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                  "function": {"name": "bash",
                                               "arguments": json.dumps({"command": command})}}]
    return litellm.ModelResponse(model="scripted", choices=[{"message": message}],
                                 usage={"prompt_tokens": 100, "completion_tokens": 10,
                                        "total_tokens": 110})


class HelperTests(unittest.TestCase):
    def test_line_diff_rebuilds_new_text(self):
        old = "\n".join(f"line {i}" for i in range(30))
        new = old.replace("line 5", "line five").replace("line 20\n", "") + "\nline 30"
        ops = line_diff(old, new)
        self.assertEqual(_apply(old, new, ops), new.splitlines())
        dels = [ln for op in ops if op["op"] == "del" for ln in op["lines"]]
        self.assertEqual(sorted(dels), ["line 20", "line 5"])

    def test_ready_command(self):
        self.assertTrue(protocol.is_ready_command("echo READY_FOR_NEXT_OP", 0))
        self.assertFalse(protocol.is_ready_command("echo READY_FOR_NEXT_OP", 1))
        self.assertFalse(protocol.is_ready_command("grep READY_FOR_NEXT_OP f", 0))

    def test_reply_text_drops_marker(self):
        self.assertEqual(protocol.reply_text("hello\nREADY_FOR_NEXT_OP\n"), "hello")


class ConfigTest(unittest.TestCase):
    def test_openrouter_only(self):
        cfg = SessionConfig.from_dict({"model": "openrouter/qwen/qwen3.5-9b"})
        self.assertEqual(cfg.model, "qwen/qwen3.5-9b")
        self.assertFalse(cfg.kwargs()["send_chat_template_kwargs"])

    def test_task_mode_uses_the_paper_settings(self):
        with self.assertRaises(ValueError):
            SessionConfig.from_dict({"mode": "task"})  # no task chosen
        kw = SessionConfig.from_dict({"mode": "task", "task": "t"}).kwargs()
        self.assertEqual(kw, {"send_chat_template_kwargs": False})


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class ShellListingTest(unittest.TestCase):
    """The one-call listing the Harbor sandboxes use, run through local bash."""

    def test_list_trees_batches_roots(self):
        from clm_ui.sandbox import ShellSandbox, _run

        class BashShell(ShellSandbox):
            async def _sh(self, command, timeout):
                return await _run([find_bash(), "-c", command], timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            (a / "sub").mkdir(parents=True)
            (a / "sub" / "f.txt").write_text("hello")
            b.mkdir()
            roots = [a.as_posix(), b.as_posix()]
            got = asyncio.run(BashShell().list_trees(roots))
            text = asyncio.run(BashShell().read_text((a / "sub" / "f.txt").as_posix()))
        self.assertEqual([e["path"] for e in got[roots[0]]],
                         [f"{roots[0]}/sub", f"{roots[0]}/sub/f.txt"])
        self.assertEqual(got[roots[0]][1]["size"], 5)
        self.assertEqual(got[roots[1]], [])
        self.assertEqual(text, "hello")


@contextlib.contextmanager
def local_sandboxes():
    """Sessions built here run in clm_ui_me's LocalSandbox instead of Daytona or Docker."""

    def make(kind, session_dir, *, task=None, name=None):
        return LocalSandbox(session_dir / "sandbox", task=task)

    with mock.patch("clm_ui.session.HarborSandbox", make):
        yield


async def _in_sandbox_paths(s: Session, body) -> None:
    """Run ``body`` with the harness pointed at the local sandbox's context folder."""
    with harness_paths(s.sandbox.ctx_dir, s.sandbox.state_dir):
        await body()


async def _wait(s: Session, *states: str, limit: float = 60.0) -> None:
    for _ in range(int(limit / 0.05)):
        await asyncio.sleep(0.05)
        if s.state in states:
            return
    raise AssertionError(f"session stuck in {s.state}")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class ChatSessionTest(unittest.TestCase):
    def test_works_with_bash_then_answers_in_plain_text(self):
        script = iter([_reply("THOUGHT: look", "echo 42 > answer.txt && cat answer.txt"),
                       _reply("The answer is 42.")] * 2)

        async def fake(self, model, messages):
            return next(script)

        with tempfile.TemporaryDirectory() as tmp, local_sandboxes(), \
                mock.patch.object(ClmAgent, "_query_with_retry", fake):
            events = asyncio.run(self._run(Path(tmp)))
        types = [e["type"] for e in events]
        self.assertNotIn("error", types)
        self.assertEqual([e["text"] for e in events if e["type"] == "reply"],
                         ["The answer is 42."] * 2)
        # The plain-text answer ends the turn: the harness's "No tool call" note never
        # reaches the model.
        sent = [t for e in events if e["type"] == "context" for t in e["turns"]]
        self.assertFalse(any(t["preview"].startswith("(No tool call") for t in sent))
        fs = [e for e in events if e["type"] == "fs"][-1]
        names = {x["path"].rsplit("/", 1)[-1] for r in fs["roots"] for x in r["entries"]}
        self.assertTrue({"LIVE_CTX_MAIN.txt", "answer.txt", "cwd"} <= names)

    async def _run(self, tmp: Path) -> list[dict]:
        s = Session(SessionConfig(model="scripted/model"), runs_dir=tmp,
                    sandbox_kind="docker", tasks_dir=None)

        async def body() -> None:
            await s.start()
            await _wait(s, "waiting_user")
            for text in ("What is the answer?", "Again?"):
                await s.send(text)
                await asyncio.sleep(0.1)
                await _wait(s, "waiting_user")
            await s.stop()

        await _in_sandbox_paths(s, body)
        return s.events


TASKS = _default_tasks_dir()
TASK = "needle-retention-K4-C4-seed42"


@unittest.skipUnless(shutil.which("bash") and TASKS and (TASKS / TASK).is_dir(),
                     "needs bash and ContextBench's cb_tasks")
class TaskSessionTest(unittest.TestCase):
    def test_streams_every_op_then_grades(self):
        async def fake(self, model, messages):
            return _reply("THOUGHT: next op", "echo READY_FOR_NEXT_OP")

        with tempfile.TemporaryDirectory() as tmp, local_sandboxes(), \
                mock.patch.object(ClmAgent, "_query_with_retry", fake):
            s = Session(SessionConfig(mode="task", task=TASK, model="scripted/model"),
                        runs_dir=Path(tmp), sandbox_kind="docker", tasks_dir=TASKS)

            async def body() -> None:
                await s.start()
                await _wait(s, "finished", limit=120)
                await s.stop()

            asyncio.run(_in_sandbox_paths(s, body))
            events = s.events
            stream = json.loads((s.logs_dir / "interactive_stream.json").read_text())
            # The run folder is laid out like a Harbor trial.
            self.assertTrue(s.dir.name.startswith(TASK + "__"))
            config = json.loads((s.dir / "config.json").read_text())
            result = json.loads((s.dir / "result.json").read_text())
            logged = (s.dir / "events.jsonl").read_text().splitlines()
            self.assertTrue((s.dir / "verifier" / "reward.json").is_file())
        self.assertEqual(config["task"], TASK)
        self.assertEqual(config["agent_kwargs"]["context_budget_tokens"], 32768)
        self.assertEqual(result["terminated_by"], "stream_exhausted")
        self.assertEqual(result["rewards"], next(
            e for e in events if e["type"] == "grade")["rewards"])
        self.assertEqual(len(logged), len(events))
        types = [e["type"] for e in events]
        self.assertNotIn("error", types)
        ops = [e for e in events if e["type"] == "user_message" and e.get("kind") == "op"]
        self.assertEqual(len(ops), 4)
        self.assertEqual([e["delivered"] for e in ops], [1, 2, 3, 4])
        self.assertEqual(stream["terminated_by"], "stream_exhausted")
        grade = next(e for e in events if e["type"] == "grade")
        self.assertIsInstance(grade["rewards"], dict)
        self.assertTrue(grade["rewards"])


if __name__ == "__main__":
    unittest.main()
