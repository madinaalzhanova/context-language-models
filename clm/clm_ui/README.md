# clm_ui: CLM Inspector

A web page that shows a CLM run as it happens. It runs the unmodified CLM loops
([`ClmAgent`](../clm_harness/), and [`ClmStream`](../clm_contextbench/) for ContextBench)
on OpenRouter models and shows three columns:

| column | shows |
|---|---|
| **Chat** | your messages and the agent's answers; for a ContextBench task, the instruction, each op as the stream delivers it, each op done, and the grade |
| **Inside CLM** | each turn of the loop: the budget check and any nudge or rollback, the context map, the model's reasoning and THOUGHT, the bash call, its output, the edit gate's verdict on a context edit (tokens before → after, the lines changed), and the tool result the model sees next |
| **File system** | the sandbox (context mirror, offloaded context in `/tmp/ctx_offload`, workdir) and the agent's logs, with a viewer that follows the context file live and marks what the model and the harness changed |

## Run

```bash
pip install -e ".[ui]"
# OPENROUTER_API_KEY and DAYTONA_API_KEY in the environment, or in a file for --env-file
clm-ui                    # Daytona; --sandbox docker for Docker
```

Open http://127.0.0.1:7860 and click **New session**. Choose:

- **Mode.** *Chat*, or *ContextBench task*.
- **OpenRouter model.** Any model OpenRouter lists with tool support (CLM acts only
  through its bash tool). The dialog shows each model's context length and price, and
  the timeline adds up the cost. Default `qwen/qwen3.5-9b`; `--model` changes it.
- **Task.** A packaged task from `--tasks-dir`; by default `cb_tasks/` of the
  ContextBench checkout that `contextbench` is imported from.

One session runs at a time; a new one stops the old one and deletes its sandbox.

Each run is saved in `tests/` (`--runs-dir`; kept out of git), one folder per run, laid
out like a Harbor trial in `harbor trials start --trials-dir`:

```text
tests/needle-retention-K4-C24-seed42__20261016-142301/   # chat runs: chat__<time>
  config.json       the run: model, task, sandbox, settings, agent kwargs
  result.json       how it ended: reason, terminated_by, rewards, duration, usage
  events.jsonl      everything the page showed, one event per line
  agent/            the agent's logs: trajectory.json, usage.json, timing.json,
                    context_snapshots/turn-NNNN.json (each LM call's exact input),
                    interactive_stream.json and clm_stream.json (tasks)
  verifier/         the grade (tasks): reward.json, contextbench_result.json
```

## ContextBench tasks

A task runs the way this command runs it:

```bash
harbor trials start -p cb_tasks/<task> -e daytona -a clm_contextbench:ClmStream \
  -m openrouter/<model> --agent-kwarg send_chat_template_kwargs=false
```

- The sandbox is Harbor's environment for the task: its image, its `/app` workdir, its
  resources, and its `environment/` directory (the op stream) uploaded into `/app`.
- The agent is `ClmStream` itself, with the paper's settings (`PAPER_KWARGS`) and the
  family's method skill. It reads the stream, checks the op count and sha against
  `task.toml`, removes it from the sandbox, and delivers the ops one at a time; the
  agent asks for each next op with `echo READY_FOR_NEXT_OP`.
- When the run ends, ContextBench's grader (`contextbench.harbor.reward`, what the
  task's `tests/test.sh` runs) grades the agent's context snapshots against
  `tests/gold.json`. The chat shows the reward; `verifier/reward.json` and
  `verifier/contextbench_result.json` hold the full result.

The page only watches: you cannot type during a task.

## Chat

`ClmAgent` with its defaults (nudges at 25/50/75%, the adaptive urgent nudge,
rollback-retry), no step limit, and a 32000-token budget (`--budget`). The agent works
with bash and answers in plain text: a turn with no tool call is its answer and ends its
turn.

When the session starts, curl and wget are installed (`python:3.12-slim` has neither) and
the sandbox's internet access is checked. Daytona's Tier 1 and 2 block the open web and
allow only package registries, GitHub and model APIs; when the web is blocked, the agent
is told to answer from its own knowledge and say that it did not search.

## The context file

The context is one file, `/tmp/.live_ctx/LIVE_CTX_MAIN.txt`. Before every command the
harness rewrites it from the message list, one block per message after the pinned prefix:

```text
[[CTX_TURN 1 role=user]]
<the user's first message>

[[CTX_TURN 2 role=assistant]]
<reasoning, THOUGHT, then the bash call as `bash {"command": ...}`>

[[CTX_TURN 3 role=tool]]
<command output>
[context: ~1850/29952 tokens]
```

The system prompt and the task are **not** in the file. The harness pins them, and they
show as hatched segments in the context map. After the command, the harness reads the
file back. If it changed, `parse_back` turns the text into messages again: tool turns
become user turns, and emptied turns are dropped. The edit gate then accepts or rejects
the edit. The file exists only after the first command, and turns added since the last
command (that command's own turn, a new op, an answer) reach it at the next command;
the viewer says so and previews them. Next to that file, `/tmp/.bash_ctx_state/` holds
the shell's `cwd` and `env`. On the host, `context_snapshots/turn-NNNN.json` records the
exact input of each LM call; ContextBench grades these.

In the viewer, green and red lines are the model's edits. Blue lines are the harness's
own writes: turns appended since the last command, turns removed by a rollback, or a
rejected edit being restored.

## Sandboxes

- `daytona` (the default): Harbor's `DaytonaEnvironment`,
  with Harbor's credentials (`DAYTONA_API_KEY`, or `DAYTONA_JWT_TOKEN` and
  `DAYTONA_ORGANIZATION_ID`). Each file listing and read of the context file is a remote
  call, so the live view of the file refreshes every 1.5 s during a command.
- `docker` (`--sandbox docker`): Harbor's Docker environment; Docker must be running.

`clm-ui` refuses to start when the chosen one is not available.

## Code

| file | contents |
|---|---|
| `agent.py` | `ChatAgent` (`ClmAgent`) and `TaskAgent` (`ClmStream`), with hooks on the loop's existing call points that report each step; no copy of the loop |
| `session.py` | sandbox, agent task, grading, event log (replayed to a page that reconnects), file listing |
| `sandbox.py` | `HarborSandbox`: Harbor's Daytona or Docker environment, with the `exec` / `upload_file` / `download_file` surface the agents use, plus file listing for the page |
| `protocol.py` | what a chat agent is told; ContextBench's readiness check |
| `server.py` | FastAPI app, OpenRouter model list, task list, `clm-ui` CLI |
| `static/` | the page (no build step) |

Tests: `PYTHONPATH=clm python -m unittest clm.tests.test_clm_ui -v`. They run the chat
loop and a ContextBench task end to end without Daytona or Docker: the OpenRouter call is
replaced by scripted replies, and the sandbox by the local one in
[`clm/tests/local_sandbox.py`](../tests/local_sandbox.py).
