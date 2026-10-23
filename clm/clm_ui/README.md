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
  context/          CONTEXT.json: the context itself at the latest LM call, the
                    message list as sent (pinned system prompt and task included)
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
  family's method skill (**Method skill** `clm`), or no skill at all (`none`: the
  model gets the task text and CLM's system prompt, and has to find its own way to
  stay under the budget). It reads the stream, checks the op count and sha against
  `task.toml`, removes it from the sandbox, and delivers the ops one at a time; the
  agent asks for each next op with `echo READY_FOR_NEXT_OP`.
- **Agent** `base model without CLM` runs the same task with no context management:
  ContextBench's append-only `ContextBenchAgent` (the paper's *Base* arm) on the same
  OpenRouter model. It keeps every message until the budget wall ends the run: no
  context file, no edit gate, no nudge. Use **Method skill** `base` (the paper's text
  for it) or `none`. The page shows its LM calls, commands and ops as for CLM.
- When the run ends, ContextBench's grader (`contextbench.harbor.reward`, what the
  task's `tests/test.sh` runs) grades the agent's context snapshots against
  `tests/gold.json`. The chat shows the reward; `verifier/reward.json` and
  `verifier/contextbench_result.json` hold the full result.

The page only watches: you cannot type during a task.

For kv_store and log_triage the page adds **`single_guess`**
([`metrics.py`](metrics.py)), a stricter score than ContextBench's `reward`. ContextBench
counts a query correct when any ANSWER the agent ever wrote for it matches, so several
guesses for one query cost nothing. `single_guess` counts a query only when the agent
gave exactly one answer for it and that answer is correct (for kv_store, also: one value
per ANSWER block). It is written to `verifier/single_guess.json` and `result.json`;
`verifier/reward.json` stays exactly what ContextBench wrote.

## Steering

**Steering skill** in New session picks a `SKILL.md` that is appended to the end of the
system prompt, through `ClmAgent`'s `skill_dirs` setting: the mechanism the paper's
in-context learning ([`clm_icl`](../clm_icl/)) evolves. The choices:

- **none**: zero-shot;
- **your skills**: `skills/<name>/SKILL.md` in the repo (`--skills-dir`);
- **evolved**: every skill of an evolution run, and its best one (`--evolutions-dir`);
- **ContextBench method texts**: `cb:<family>`, the paper's per-family method text. With
  Method skill = `none`, this moves that text from the task instruction (a user message)
  to the end of the system prompt: a placement the paper did not test, worth comparing;
- **custom**: text you type in the dialog.

The page offers only these; it never reads another path. The run saves the exact text as
`skill/SKILL.md` and records it in `config.json`; the **skill** button above the context
file shows it during the run. In a ContextBench task, steering is separate from **Method
skill** (`clm` / `none`), ContextBench's per-family method text in the task instruction.

## Evolution

**Evolution** (top bar) runs `clm_icl`'s loop on ContextBench tasks and shows it as it
goes. Each round:

1. the current skill runs on the **training tasks**;
2. a note contrasts its better and worse runs, and a proposer writes new candidate
   skills: by default the agent's own model (**self-evolution**), or a **teacher** model
   you choose, usually a stronger one;
3. every candidate runs on the **development tasks**;
4. the gate keeps a candidate only if its mean score is higher by more than one standard
   error, or equal and cheaper.

The loop is `clm_icl.evolve.Evolution`, unchanged. Each task run ("episode") is a
ContextBench task run exactly as a task session runs it (`python -m clm_ui.episode`, one
process per run, on the same sandbox), scored by `reward` or `single_guess`, with its cost
in USD at OpenRouter's prices. The view shows the skills (score, graded runs, cost, the
gate's reason), each skill's changes from the one it was proposed to replace, the runs the
proposer compared and its analysis per round, and the loop's log.

A run lives in `evolutions/<name>/` (kept out of git): `state.json`, `skills/<id>/SKILL.md`,
`rounds/r<k>/`, `episodes/`, `trials/` (every task run's folder), `evolution.log` and, at
the end, `best_skill/SKILL.md`. Starting a run with an existing name resumes it: graded
runs are reused. **Stop** ends the running task runs and the loop. **Use in a new
session** steers a session with the selected skill.

Runs add up: with T training and D development tasks, R rounds, C candidates and N
repeats, a run needs up to N x (D + R x (T + C x D)) task runs; the form shows the number.
Give it development tasks: without them, skills are selected on the tasks the proposer
read. `http://127.0.0.1:7860/#evolution/<name>` opens a run directly.

## Benchmark

**Benchmark** (top bar) runs the same ContextBench tasks under several **arms** and
compares them, so you no longer compare `result.json` files by hand. An arm is an agent
(CLM, or the base model without CLM), an OpenRouter model, ContextBench's method text and
a steering skill; **CLM vs base model** fills in the usual pair. Every arm runs every task
N times (**repeats**); each run is one `python -m clm_ui.episode` process, the task
runner evolution uses, up to **runs in parallel** at once.

- **Results**: per arm, the mean ± standard error (sample sd / √n, over graded runs) of
  the score (`reward`, and `single_guess` for kv_store and log_triage), the difference
  in score from the first arm (± the standard error of a difference of independent
  means), peak context tokens (the grader's `agent_peak_tokens`), context edits (CLM's
  non-trivial edits, `n_ctx_syncs_real`; the baseline makes none), cost per run (USD at
  OpenRouter's prices), the share of ops the run reached before it ended, and wall time.
  **Score by task** splits the score per task.
- **Running now**: one tile per running episode, side by side: its context tokens per
  turn against the limit (CLM's saw-tooth as it edits next to the baseline's climb to the
  wall), ops delivered, LM calls, edits and the last command. The page follows each
  run's `events.jsonl` as the episode writes it.
- **All runs**: tasks × arms, one square per repeat with its score. Click one (or a tile)
  to follow that run: its context-tokens chart and its recent ops, thoughts, commands and
  context edits.

A benchmark lives in `benchmarks/<name>/` (`--benchmarks-dir`; kept out of git):
`config.json`, `benchmark.log`, `skills/<arm>/skill/SKILL.md`, `trials/<arm>/<task>__r<k>/`
(every run's folder, as a session's) and `results/<arm>/<task>__r<k>.json` (its numbers).
Starting a benchmark with an existing name resumes it: graded runs are kept, the rest run
again. **Stop** ends the running episodes. Right-click the benchmark picker to delete the
benchmark it shows (refused while it runs).

Arms run **in pairs** (the default; a checkbox in the form): the runs of one task and
repeat, one per arm (`clm r0` and `base r0`), start together, and the next pair starts
only once the whole pair has finished, so a faster arm never runs ahead. Up to
N / arms pairs run at once.

With **runs in parallel** = N, N runs start together and each creates its own sandbox at
the same time; when one ends it deletes its sandbox and the next run starts with a new
one. If Daytona is at its limit (sandbox count, CPU or memory of your tier, including
sandboxes of an open inspector session), it refuses the extra ones and Harbor retries
them with growing waits; those runs stay at "starting the daytona sandbox" meanwhile. `#benchmark/<name>` opens a benchmark and
`#benchmark/<name>/<arm>/<task>__r<k>` one of its runs.

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

The file is only the editable part of the context. The context itself is the whole
message list the model receives: the pinned system prompt and task, then the turns. The
**context itself** folder of the File system column holds it, as `context/CONTEXT.json` in
the run folder, rewritten before every LM call: the message list exactly as sent, the
same as `messages` in that call's `context_snapshots/turn-NNNN.json`.

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

Every run gets a fresh sandbox and deletes it when it no longer needs it, so none is
left running:

- **A task session** that has ended (and been graded) copies the folders the
  file-system column shows to `<run>/sandbox/` and deletes its sandbox; the column then
  shows the copies.
- **Closing the page**: once no page has been open for `--close-grace-s` seconds
  (default 120; a reload within it keeps the session), the session is stopped and its
  sandbox deleted. Benchmarks and evolutions keep running: they delete their own.
- **Stop** (benchmark, evolution) and shutting the server down send each running
  episode a stop signal (CTRL_BREAK_EVENT on Windows, SIGTERM elsewhere): it cancels its
  run and deletes its sandbox before it exits, within 90 s, else it is killed. The
  server waits for this before it exits.
- **A safety net**: Daytona deletes a sandbox idle for `--sandbox-idle-mins` minutes
  (default 30; 0 turns it off), for one whose process was killed before it could. A chat
  left waiting longer than that loses its sandbox.

A sandbox is never reused between runs: a run's files (offloaded context, answers,
notes) would be visible to the next run of the same task.

## Code

| file | contents |
|---|---|
| `agent.py` | `ChatAgent` (`ClmAgent`) and `TaskAgent` (`ClmStream`), with hooks on the loop's existing call points that report each step; no copy of the loop. `BaselineAgent`: ContextBench's append-only agent (no CLM), observed the same way |
| `session.py` | sandbox, agent task, grading, event log (replayed to a page that reconnects), file listing |
| `sandbox.py` | `HarborSandbox`: Harbor's Daytona or Docker environment, with the `exec` / `upload_file` / `download_file` surface the agents use, plus file listing for the page |
| `protocol.py` | what a chat agent is told; ContextBench's readiness check |
| `metrics.py` | `single_guess`, a stricter kv_store / log_triage score that ContextBench's graders, reused, do not compute |
| `skills.py` | the steering skill library (`skills/`, evolution runs) |
| `evolution.py` | the `clm_icl` task source for ContextBench, the background job, and what the Evolution view reads |
| `episode.py` | one ContextBench task run without the page (`python -m clm_ui.episode`), and the process runner evolution and benchmark share |
| `benchmark.py` | arms x tasks x repeats: the background job, mean ± standard error per arm, and the live tail of each running episode |
| `server.py` | FastAPI app, OpenRouter model list, task and skill lists, evolution and benchmark APIs, `clm-ui` CLI |
| `static/` | the page (no build step) |

Tests: `PYTHONPATH=clm python -m unittest discover -s clm/tests -v`. They run the chat
loop and a ContextBench task end to end without Daytona or Docker: the OpenRouter call is
replaced by scripted replies, and the sandbox by the local one in
[`clm/tests/local_sandbox.py`](../tests/local_sandbox.py).
