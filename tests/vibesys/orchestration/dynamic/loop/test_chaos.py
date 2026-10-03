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

# Seeds 2018 and 2022 completed with zero workstreams before #1228. Each seed runs
# a whole loop (10 to 20 s on a CI runner), so a pull request runs four generic
# seeds beside those two. The nightly workflow runs 0-11 and sweeps many more.
_PR_SEEDS = "0-3,2018,2022"


def _seeds() -> list[object]:
    spec = os.environ.get("CHAOS_SEEDS", _PR_SEEDS)
    seeds: list[object] = []
    for part in spec.split(","):
        low, _, high = part.partition("-")
        seeds.extend(
            pytest.param(seed, id=f"seed_{seed}") for seed in range(int(low), int(high or low) + 1)
        )
    return seeds


@pytest.mark.parametrize("seed", _seeds())
def test_the_loop_keeps_its_invariants_under_generated_agents_and_faults(
    tmp_path: Path, seed: int
) -> None:
    chaos = run_chaos(tmp_path, seed)

    assert chaos.violations == [], chaos.report()


def test_no_evaluation_is_submitted_after_a_stop_during_a_profile(tmp_path: Path) -> None:
    """Seed 4025 stops the run while a profile runs and a turn then submits an evaluation."""
    chaos = run_chaos(tmp_path, 4025)

    assert chaos.violations == [], chaos.report()
