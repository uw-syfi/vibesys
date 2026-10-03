"""Chaos sweep of the dynamic loop: generated agents under seeded fault plans.

The PR tier runs a fixed set of seeds. ``CHAOS_SEEDS=A-B`` (or a comma list)
runs other seeds; ``scripts/chaos_dynamic_loop.sh N`` sweeps N seeds in
parallel. A failure prints its seed, the injected faults, the violations, and
a one-line repro.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.loop._chaos import run_chaos

if TYPE_CHECKING:
    from pathlib import Path

_PR_SEEDS = "0-49"


def _seeds() -> list[int]:
    spec = os.environ.get("CHAOS_SEEDS", _PR_SEEDS)
    seeds: list[int] = []
    for part in spec.split(","):
        low, _, high = part.partition("-")
        seeds.extend(range(int(low), int(high or low) + 1))
    return seeds


@pytest.mark.parametrize("seed", _seeds(), ids=lambda seed: f"seed_{seed}")
def test_the_loop_keeps_its_invariants_under_generated_agents_and_faults(
    tmp_path: Path, seed: int
) -> None:
    chaos = run_chaos(tmp_path, seed)

    assert chaos.violations == [], chaos.report()
