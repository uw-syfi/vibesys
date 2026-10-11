"""Every strategy's prompt templates show the run environment's notes.

A strategy wires `RunFacts.environment_notes` into its templates by hand, so a new
strategy can forget it and its agents never learn how commands run in this environment
(live-slurm: the dynamic loop did). The per-prompt guarantee for the dynamic loop is
tested where its prompts are rendered; this guard fails for any strategy directory with
no template that reads the notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ORCHESTRATION = Path(__file__).resolve().parents[2] / "src" / "vibesys" / "orchestration"
_NOTES = re.compile(r"\b(runtime_notes|environment_notes)\b")
_PROMPT_DIRS = sorted(
    path
    for path in (*_ORCHESTRATION.glob("*/prompts"), *_ORCHESTRATION.glob("*/*/prompts"))
    if any(path.rglob("*.j2"))
)


def test_the_guard_covers_every_strategy() -> None:
    names = {path.relative_to(_ORCHESTRATION).parts[0] for path in _PROMPT_DIRS}

    assert {"dynamic", "evolve", "issue_queue", "multi", "single"} <= names


@pytest.mark.parametrize(
    "prompts", _PROMPT_DIRS, ids=lambda path: str(path.relative_to(_ORCHESTRATION))
)
def test_a_strategy_prompt_directory_reads_the_environment_notes(prompts: Path) -> None:
    readers = [t for t in prompts.rglob("*.j2") if _NOTES.search(t.read_text())]

    assert readers, f"no template under {prompts} reads the run environment's notes"
