# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""What the agent is told in a chat session, and how a ContextBench op ends.

In chat, user messages arrive one at a time as user turns, and the agent replies with
a turn that calls no tool. In a ContextBench task, ``ClmStream`` delivers the ops and
the agent asks for the next one with ``echo READY_FOR_NEXT_OP``; ContextBench's own
matcher decides whether a command did that.
"""

from __future__ import annotations

from contextbench import stream as _cb

ADVANCE_MARKER = _cb.ADVANCE_MARKER

#: The system prompt's finish paragraph in chat.
CHAT_PARAGRAPH = (
    "You are in a conversation with a user, who sends one message at a time. Use bash\n"
    "while you work on their message. When you are done, reply to the user in plain\n"
    "text with NO tool call: that message is shown to the user and ends your turn (this\n"
    "is the one exception to the rule that every response calls `bash`). Do not keep\n"
    "working after you have answered, and do not invent extra work; the user's next\n"
    "message arrives as a new turn. Do not submit: the user ends the conversation."
)

#: Stands in for the task in chat; the user's messages are appended after it.
CHAT_TASK = (
    "Help the user with whatever they ask. Their messages arrive as user turns, one at a "
    "time. You have a bash shell; use it when it helps."
)

#: Appended to the chat task when the sandbox cannot reach the open web.
NO_WEB_NOTE = (
    "This sandbox has no open internet: web pages and search APIs are blocked; only "
    "package registries (pip, apt) are reachable. Do not try to fetch web pages. When a "
    "request needs the web, answer from your own knowledge and say plainly that you did "
    "not search."
)


def is_ready_command(command: str, exit_code: int) -> bool:
    """True when ``command`` runs ``echo READY_FOR_NEXT_OP`` as a whole statement."""
    return _cb.is_ready_command(command, exit_code)


def reply_text(stdout: str) -> str:
    """A command's output with the marker lines removed."""
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip() != ADVANCE_MARKER]
    return "\n".join(lines).strip()
