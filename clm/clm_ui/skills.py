# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Steering skills: the ``SKILL.md`` texts a run can be steered with.

A skill is a folder with a ``SKILL.md``. ``ClmAgent`` takes it through its
``skill_dirs`` kwarg and appends the text to the end of the system prompt
(``clm_harness.utils.skills.SkillMount``), the mechanism ``clm_icl`` evolves skills
with. The library lists the skills the page may offer:

* ``lib:<name>``: ``<skills_dir>/<name>/SKILL.md``, skills you keep;
* ``evo:<run>/best``, ``evo:<run>/<id>``: ``<evolutions_dir>/<run>/best_skill/SKILL.md``
  and each candidate ``<run>/skills/<id>/SKILL.md`` of an evolution run;
* ``cb:<family>``: ContextBench's CLM method text for a family
  (``clm_exp/skills/clm/<family>.md``), the text a task session inserts into the task
  instruction with Method skill = clm. As a steering skill it goes to the end of the
  system prompt instead (use it with Method skill = none).

The page names a skill by its id; no other path is ever read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_FILE = "SKILL.md"


@dataclass
class SkillLibrary:
    skills_dir: Path | None = None
    evolutions_dir: Path | None = None
    method_dir: Path | None = None      # ContextBench's clm_exp/skills/clm

    def entries(self) -> list[dict[str, Any]]:
        """Every skill on offer: id, a label for the page, and its text's size."""
        out: list[dict[str, Any]] = []
        if self.skills_dir and self.skills_dir.is_dir():
            for f in sorted(self.skills_dir.glob(f"*/{SKILL_FILE}")):
                out.append(self._entry(f"lib:{f.parent.name}", f.parent.name, f))
        if self.method_dir and self.method_dir.is_dir():
            for f in sorted(self.method_dir.glob("*.md")):
                out.append(self._entry(f"cb:{f.stem}", f"ContextBench method text: {f.stem}", f))
        if self.evolutions_dir and self.evolutions_dir.is_dir():
            for run in sorted(p for p in self.evolutions_dir.iterdir() if p.is_dir()):
                best = run / "best_skill" / SKILL_FILE
                if best.is_file():
                    out.append(self._entry(f"evo:{run.name}/best", f"{run.name}: best", best))
                for f in sorted((run / "skills").glob(f"*/{SKILL_FILE}")):
                    out.append(self._entry(f"evo:{run.name}/{f.parent.name}",
                                           f"{run.name}: {f.parent.name}", f))
        return out

    @staticmethod
    def _entry(skill_id: str, label: str, path: Path) -> dict[str, Any]:
        return {"id": skill_id, "label": label, "chars": path.stat().st_size}

    def path(self, skill_id: str) -> Path:
        """The SKILL.md behind an id the library lists; anything else is refused."""
        kind, _, rest = skill_id.partition(":")
        if kind == "lib" and self.skills_dir:
            path = self.skills_dir / rest / SKILL_FILE
        elif kind == "evo" and self.evolutions_dir:
            run, _, cand = rest.partition("/")
            path = (self.evolutions_dir / run /
                    ("best_skill" if cand == "best" else f"skills/{cand}") / SKILL_FILE)
        elif kind == "cb" and self.method_dir:
            path = self.method_dir / f"{rest}.md"
        else:
            raise ValueError(f"unknown skill {skill_id!r}")
        roots = [r.resolve() for r in (self.skills_dir, self.evolutions_dir, self.method_dir) if r]
        path = path.resolve()
        if not path.is_file() or not any(path.is_relative_to(r) for r in roots):
            raise ValueError(f"no skill {skill_id!r}")
        return path

    def text(self, skill_id: str) -> str:
        return self.path(skill_id).read_text(encoding="utf-8")


def mount_dir(run_dir: Path, text: str) -> Path:
    """Write the skill a run uses to ``<run_dir>/skill/SKILL.md`` and return that folder:
    the run keeps the exact text it was steered with, and only this folder is uploaded
    into the sandbox."""
    d = run_dir / "skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_FILE).write_text(text, encoding="utf-8")
    return d
