"""Byte-exact goldens for operator constraints added to a run's objective.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as
a prompt diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vibesys.api.request import with_operator_constraints

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "operator_constraints"

_CASES = {
    "two_constraints": ("Maximize throughput.\n", ["No quantization.", "  One H100 only.  "]),
    "blank_entries_dropped": ("Maximize throughput.\n\n\n", ["", "   ", "Keep bf16."]),
    "multiline_objective": ("# Goal\n\nServe Llama.\n\n## Rules\n- exact", ["No FP8."]),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_operator_constraints_golden(case: str) -> None:
    objective, constraints = _CASES[case]
    actual = with_operator_constraints(objective, constraints)
    path = _SNAPSHOT_DIR / f"{case}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        path.write_text(actual, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == actual


@pytest.mark.parametrize("constraints", [[], ["", "  \n "]])
def test_no_constraints_keep_the_objective(constraints: list[str]) -> None:
    assert with_operator_constraints("Maximize throughput.\n", constraints) == (
        "Maximize throughput.\n"
    )
