# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""The unmodified CLM loops, observed: ``ChatAgent`` (``ClmAgent``) and ``TaskAgent``
(``clm_contextbench.ClmStream``).

The loop is not copied. Like ``ClmStream``, ``_Inspected`` hooks points the loop already
calls, and each hook reports what it saw before or after delegating:

| hook | reports |
|---|---|
| ``_budget.evaluate`` | token count against the limit, nudges, rollback / final turn |
| ``_snap`` | the exact message list sent to the model (the context map) |
| ``_query_with_retry`` | the request, then reasoning, THOUGHT and the bash call |
| ``_ctx.write_mirror`` | the context file as written before the command |
| ``_ctx.execute`` | the command, its output, and the context file while it runs |
| ``_ctx.step`` | the edit read back, the gate's verdict, and the observation |
| ``_ckpt.maybe`` | top of each loop iteration (``_before_turn``) |

``ChatAgent`` adds the one behavioural change: it waits for the user's next message
and treats a turn with no tool call as its reply. ``TaskAgent`` changes nothing:
``ClmStream`` delivers the task's ops, and the hooks only report them.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from typing import Any, Awaitable, Callable

from clm_contextbench.clm_stream import ClmStream
from clm_harness.clm_agent.harness import ClmAgent
from clm_harness.context_utils.context_string import render_editable, rendered_text
from clm_harness.utils import tokens as tk
from clm_harness.utils.finish_policy import FinishPolicy

from . import protocol

Emit = Callable[..., None]

#: Start of the note ClmAgent appends after a turn with no tool call.
_NO_TOOL_NOTE = "(No tool call in your last turn."
#: Characters of a command's raw stdout/stderr sent to the page.
MAX_STREAM_CHARS = 20000


class _Finish(FinishPolicy):
    """``submit`` mode whose system-prompt paragraph is the chat contract."""

    __slots__ = ("paragraph",)

    def __init__(self, mode: str, paragraph: str) -> None:
        super().__init__(mode)
        self.paragraph = paragraph

    def prompt_fragment(self) -> str:
        return self.paragraph


def line_diff(old: str, new: str) -> list[dict[str, Any]]:
    """Line ops turning ``old`` into ``new``. ``del`` / ``add`` carry their lines; an
    ``eq`` run carries only its length ``n`` and its first and last two lines, since
    the page has the new text to fill it from."""
    a, b = old.splitlines(), new.splitlines()
    ops: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            ops.append({"op": "eq", "n": i2 - i1, "head": a[i1:min(i1 + 2, i2)],
                        "tail": a[max(i2 - 2, i1 + 2):i2]})
            continue
        if tag in ("replace", "delete"):
            ops.append({"op": "del", "lines": a[i1:i2]})
        if tag in ("replace", "insert"):
            ops.append({"op": "add", "lines": b[j1:j2]})
    return ops


def _edit_status(notes: str, changed: bool) -> str:
    if "edit REJECTED" in notes:
        return "rejected"
    if "STILL OVER" in notes:
        return "applied_over"
    if "GREW" in notes:
        return "grew"
    if "edit applied" in notes:
        return "applied"
    if "NO change" in notes:
        return "no_match"
    return "applied" if changed else "none"


def _clip(text: str, n: int = MAX_STREAM_CHARS) -> str:
    return text if len(text) <= n else text[:n] + f"\n... [{len(text) - n} more chars]"


def _call_of(msg: dict[str, Any]) -> tuple[str, str]:
    """(tool name, command) of the first tool call, as ``_extract_tool_call`` reads it."""
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return "", ""
    fn = (tcs[0] or {}).get("function") or {}
    args = fn.get("arguments")
    try:
        args = json.loads(args) if isinstance(args, str) else (args or {})
    except json.JSONDecodeError:
        return fn.get("name", ""), str(args)
    return fn.get("name", ""), str(args.get("command", "")) if isinstance(args, dict) else ""


class _Inspected:
    """Observation hooks, mixed in ahead of ``ClmAgent`` or ``ClmStream``. Each reports a
    step to ``emit``; the subclass decides what ``_before_turn`` does."""

    def __init__(self, *args: Any, emit: Emit, sandbox: Any,
                 refresh_fs: Callable[[], Awaitable[None]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.emit = emit
        self.sandbox = sandbox
        self.refresh_fs = refresh_fs
        self._messages: list[dict[str, Any]] | None = None
        self._written = ""
        self._read: str | None = None
        self._last_content = ""
        self._hook()

    async def _before_turn(self, messages: list[dict[str, Any]]) -> None:
        """Top of each loop iteration, before the budget check and the LM call."""

    def _on_response(self, tool_name: str) -> None:
        """After each LM response, with the tool it called ("" for none)."""

    # ------------------------------------------------------------ wiring
    def _hook(self) -> None:
        ctx, budget, ckpt = self._ctx, self._budget, self._ckpt
        write_mirror, read_mirror = ctx.write_mirror, ctx.read_mirror
        execute, step = ctx.execute, ctx.step
        evaluate, rollback = budget.evaluate, budget.rollback_to_margin
        maybe = ckpt.maybe

        async def _maybe(environment: Any, messages: list[dict[str, Any]]) -> None:
            if self._messages is None:
                self.emit("prefix", system=messages[0].get("content", ""),
                          task=messages[1].get("content", ""), path=self.sandbox.ctx_file)
            self._messages = messages
            await self._before_turn(messages)
            await maybe(environment, messages)

        def _evaluate(messages: list[dict[str, Any]]) -> Any:
            dec = evaluate(messages)
            self.emit("budget", tokens=dec.token_count, limit=budget.strict_target,
                      budget=budget.budget, action=dec.action,
                      nudge_ratios=list(budget.nudge_ratios),
                      notices=[m.get("content", "") for m in dec.messages])
            return dec

        def _rollback(messages: list[dict[str, Any]], *a: Any, **kw: Any) -> Any:
            dropped, after = rollback(messages, *a, **kw)
            self.emit("rollback", dropped=len(dropped), tokens_after=after,
                      retry=budget.n_retry_on_limit, max_retry=budget.max_num_retry_on_limit)
            return dropped, after

        async def _write_mirror(environment: Any, messages: list[dict[str, Any]]) -> str:
            text = await write_mirror(environment, messages)
            # What the harness changed since the file was last read back: the turns
            # appended since, or the turns a rollback removed.
            prev = self._read if self._read is not None else self._written
            self._written = text
            self.emit("mirror_write", path=self.sandbox.ctx_file, text=text,
                      turns=len(messages) - self._protect, diff=line_diff(prev, text))
            return text

        async def _read_mirror(environment: Any) -> str | None:
            self._read = await read_mirror(environment)
            return self._read

        async def _execute(command: str, environment: Any) -> Any:
            self.emit("status", state="running")
            self.emit("command_start", command=command)
            poll = asyncio.create_task(self._watch_mirror())
            t0 = time.monotonic()
            try:
                res = await execute(command, environment)
            finally:
                poll.cancel()
            self.emit("command_result", stdout=_clip(res.stdout or ""),
                      stderr=_clip(res.stderr or ""), return_code=res.return_code,
                      exec_s=round(time.monotonic() - t0, 2))
            return res

        async def _step(command: str, messages: list[dict[str, Any]], **kw: Any) -> Any:
            self._read = None
            obs = await step(command, messages, **kw)
            read_back = self._read
            notes = obs.notes.strip()
            m = re.search(r"~(\d+)->(\d+)", notes)
            self.emit(
                "ctx_edit",
                status=_edit_status(notes, obs.ctx_changed),
                touched=obs.touched_ctx, applied=obs.ctx_changed, note=notes,
                before=int(m.group(1)) if m else None, after=int(m.group(2)) if m else None,
                diff=line_diff(self._written, read_back or self._written),
                text=read_back if read_back is not None else self._written,
                readout=obs.readout.strip(), observation=_clip(obs.render()),
            )
            await self.refresh_fs()
            return obs

        ctx.write_mirror, ctx.read_mirror = _write_mirror, _read_mirror
        ctx.execute, ctx.step = _execute, _step
        budget.evaluate, budget.rollback_to_margin = _evaluate, _rollback
        ckpt.maybe = _maybe

    async def _watch_mirror(self) -> None:
        """Re-read the context file while a command runs and report each change."""
        seen = self._written
        while True:
            await asyncio.sleep(self.sandbox.live_poll_s)
            text = await self.sandbox.read_text(self.sandbox.ctx_file)
            if text is not None and text != seen:
                self.emit("mirror_live", text=text, diff=line_diff(self._written, text))
                seen = text

    # ------------------------------------------------------------- hooks
    def _snap(self, messages: list[dict[str, Any]], kind: str) -> None:
        super()._snap(messages, kind)
        turns = []
        for i, m in enumerate(messages):
            pinned = i < self._protect
            label = ("system prompt" if i == 0 else "task" if i == 1 else "rollback ledger"
                     ) if pinned else f"CTX_TURN {i - self._protect + 1}"
            turns.append({"index": i, "role": m.get("role"), "pinned": pinned, "label": label,
                          "tokens": tk.count_tokens([m])[0],
                          "preview": rendered_text(m)[:240]})
        # What the context file holds for this message list. The harness only writes the
        # file before a bash command, so until the first command it does not exist.
        self.emit("context", kind=kind, snapshot=self._snap_step - 1, turns=turns,
                  tokens=self._budget.count(messages), limit=self._budget.strict_target,
                  protect=self._protect, rendered=render_editable(messages, self._protect))

    async def _query_with_retry(self, model: str, messages: list[dict[str, Any]]) -> Any:
        self.emit("status", state="thinking")
        self.emit("llm_request", call=self.n_lm_calls, model=model, n_messages=len(messages),
                  tokens=tk.count_tokens(messages)[0])
        t0 = time.monotonic()
        response = await super()._query_with_retry(model, messages)
        msg = response.choices[0].message.model_dump()
        reasoning = msg.get("reasoning_content") or "\n\n".join(
            str(b.get("thinking", "")) for b in (msg.get("thinking_blocks") or [])
            if isinstance(b, dict))
        name, command = _call_of(msg)
        self._last_content = msg.get("content") or ""
        self._on_response(name)
        usage = getattr(response, "usage", None)
        self.emit("llm_response", call=self.n_lm_calls, reasoning=reasoning or "",
                  content=self._last_content, tool=name, command=command,
                  latency_s=round(time.monotonic() - t0, 2),
                  prompt_tokens=getattr(usage, "prompt_tokens", None),
                  completion_tokens=getattr(usage, "completion_tokens", None))
        return response


class ChatAgent(_Inspected, ClmAgent):
    """``ClmAgent`` in a conversation: user turns come from ``inbox``, and a turn with no
    tool call is the reply that hands the conversation back to the user."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.finish_policy = _Finish(self.finish_policy.mode, protocol.CHAT_PARAGRAPH)
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.waiting = True              # the first message has not arrived
        self._plain_reply: str | None = None

    def _on_response(self, tool_name: str) -> None:
        if not tool_name and self._last_content.strip():
            self._plain_reply = self._last_content.strip()

    async def _before_turn(self, messages: list[dict[str, Any]]) -> None:
        if self._plain_reply is not None:
            # The last turn answered in plain text. The loop appended its "(No tool
            # call ...)" note; that turn was the reply, so drop the note.
            if (messages and messages[-1].get("role") == "user"
                    and str(messages[-1].get("content", "")).startswith(_NO_TOOL_NOTE)):
                messages.pop()
            self.emit("ctx_preview", rendered=render_editable(messages, self._protect))
            self.emit("reply", text=self._plain_reply, mode="chat")
            self._plain_reply = None
            self.waiting = True
        if self.waiting:
            self.emit("status", state="waiting_user")
            text = await self.inbox.get()
            messages.append({"role": "user", "content": text})
            self.waiting = False
            self.emit("user_turn", text=text, index=len(messages) - 1)


class TaskAgent(_Inspected, ClmStream):
    """``ClmStream``, the paper's ContextBench adapter, observed. It loads the task's op
    stream from the sandbox and delivers the ops itself; this class only reports each op
    and each ``echo READY_FOR_NEXT_OP`` to the page."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._announced = False

    def ops(self) -> dict[str, Any]:
        src = self.cb_turn_source
        return {"delivered": src.n_delivered, "total": src.n_total} if src else {}

    async def _before_turn(self, messages: list[dict[str, Any]]) -> None:
        if not self._announced:  # op 0, appended by ClmStream before the first LM call
            self._announced = True
            for m in messages[self._protect:]:
                if m.get("role") == "user":
                    self.emit("user_message", text=str(m.get("content", "")), kind="op",
                              **self.ops())

    def _is_successful_submit(self, command: str, result: Any, tool_content: str) -> bool:
        msgs = self._cb_messages
        n0 = len(msgs) if msgs is not None else 0
        before = self.ops().get("delivered", 0)
        done = super()._is_successful_submit(command, result, tool_content)
        if msgs is None:
            return done
        rc = getattr(result, "return_code", 1)
        if self.ops().get("delivered", 0) > before or (
                done and protocol.is_ready_command(command, rc)):
            self.emit("reply", text=protocol.reply_text(getattr(result, "stdout", "") or ""),
                      mode="task", **self.ops())
        for m in msgs[n0:]:  # the next op, or ContextBench's explanation or refusal
            self.emit("user_message", text=str(m.get("content", "")), kind="op", **self.ops())
        return done
