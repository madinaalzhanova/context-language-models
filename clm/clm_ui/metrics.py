# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""``single_guess``: a stricter score for kv_store and log_triage than ContextBench's.

ContextBench's graders collect every ANSWER block the agent ever wrote for a query,
across all context snapshots, into a set, and count the query correct when ANY of them
matches (``contextbench/graders/kv.py`` lines 64 and 86, ``log.py`` lines 51, 58 and
119). An agent that answers the same GET with three different values is scored as right.

``single_guess`` counts a query only when the agent committed to one answer:

* exactly one distinct ANSWER text for the query across the whole run (the same block
  seen again in later snapshots is still one answer), and
* that answer passes the family's own matching rule, unchanged, and
* kv_store only: it names one value. Every value ends in a ``#<hex>`` tag, so an
  answer carrying two or more tags lists several values in one block.
  (log_triage counts already reject "5 or 6" in one block; ContextBench's rule.)

A query the agent rewrote while compacting its context (the block's text changed) also
counts as more than one answer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from contextbench.graders.base import normalize, read_context
from contextbench.graders.kv import KVStoreGrader
from contextbench.graders.log import LogTriageGrader, _count_correct, _lookup_correct

#: The families whose grading accepts several guesses.
FAMILIES = ("kv_store", "log_triage")

#: The uniqueness tag every kv_store value ends with.
_VALUE_TAG = re.compile(r"#[0-9a-f]{6,}")


def single_guess(agent_dir: Path | str, gold: dict) -> dict[str, Any] | None:
    """``single_guess`` rewards for one run, or None for a family it does not apply to.

    The family is ``gold["meta"]["task_family"]``, which is how ContextBench picks its
    grader too."""
    family = (gold.get("meta") or {}).get("task_family")
    if family not in FAMILIES:
        return None
    queries = gold.get("queries") or []
    source = read_context(agent_dir)
    if family == "kv_store":
        answers = KVStoreGrader._collect(source)

        def key(q: dict) -> str:
            return normalize(str(q["key"]))

        def correct(q: dict, answer: str) -> bool:
            # kv.py line 64, applied to this one answer; plus one value per block.
            return (normalize(str(q["value"])) in answer
                    and len(_VALUE_TAG.findall(answer)) <= 1)
    else:
        answers = LogTriageGrader._collect(source)

        def key(q: dict) -> str:
            return str(q["qid"])

        def correct(q: dict, answer: str) -> bool:
            want = normalize(str(q["answer"]))
            if q.get("type", "lookup") == "count":
                return _count_correct(want, {answer})   # log.py line 51, one answer
            return _lookup_correct(want, {answer})       # log.py line 58, one answer

    matched = multi = 0
    for q in queries:
        seen = answers.get(key(q)) or set()
        if len(seen) > 1:
            multi += 1
        elif len(seen) == 1 and correct(q, next(iter(seen))):
            matched += 1
    total = len(queries)
    return {"single_guess": matched / total if total else 0.0,
            "single_guess_matched": float(matched),
            "n_multi_guess": float(multi)}
