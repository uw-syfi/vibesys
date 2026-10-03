"""Byte-exact goldens for the live operator steering spliced into an agent prompt.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as
a prompt diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vibesys.steering import splice_steering

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "steering"

_CASES = {
    "one_message": ("Do the work.\n", ["focus on latency"]),
    "trailing_whitespace": ("Do the work  \n\n", ["focus on latency", "check reward hacking"]),
    "multiline_message": (
        "## Task\n\nShip it.",
        ["first line\nsecond line", "  padded  "],
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_steering_golden(case: str) -> None:
    prompt, messages = _CASES[case]
    actual = splice_steering(prompt, messages)
    path = _SNAPSHOT_DIR / f"{case}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        path.write_text(actual, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == actual
