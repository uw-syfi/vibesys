"""Chaos sweep of the dynamic loop: generated agents under seeded fault plans.

The PR tier runs a fixed set of seeds plus every seed in
``chaos_regressions.txt`` (a seed that once failed; the nightly summary prints
the line to append). ``CHAOS_SEEDS=A-B`` (or a comma list) adds other seeds; ``scripts/chaos_dynamic_loop.sh N`` sweeps N seeds in
parallel. A failure prints its seed, the injected faults, the violations, and
a one-line repro.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.vibesys.orchestration.dynamic.loop._chaos import run_chaos

from vibesys.api import RunStatus

# Each seed runs a whole loop (10 to 20 s on a CI runner), so a pull request runs
# four generic seeds beside the regression seeds. The nightly workflow runs 0-11
# and sweeps many more.
_PR_SEEDS = "0-3"

_REGRESSIONS = Path(__file__).with_name("chaos_regressions.txt")


def _regression_seeds() -> list[int]:
    """Return the seeds checked in as past failures: one per line, ``#`` comments."""
    lines = (line.partition("#")[0].strip() for line in _REGRESSIONS.read_text().splitlines())
    return [int(line) for line in lines if line]


def _seeds() -> list[object]:
    spec = os.environ.get("CHAOS_SEEDS", _PR_SEEDS)
    seeds = set(_regression_seeds())
    for part in spec.split(","):
        low, _, high = part.partition("-")
        seeds.update(range(int(low), int(high or low) + 1))
    return [pytest.param(seed, id=f"seed_{seed}") for seed in sorted(seeds)]


@pytest.mark.parametrize("seed", _seeds())
def test_the_loop_keeps_its_invariants_under_generated_agents_and_faults(
    tmp_path: Path, seed: int
) -> None:
    chaos = run_chaos(tmp_path, seed)

    assert chaos.violations == [], chaos.report()


def test_no_evaluation_is_submitted_after_a_stop_during_a_profile(tmp_path: Path) -> None:
    """Seed 4025 stops the run while a profile runs and a turn then submits an evaluation."""
    chaos = run_chaos(tmp_path, 4025)

    assert chaos.run is not None
    assert chaos.run.status is RunStatus.STOPPED
    assert chaos.run.error is None
    assert chaos.violations == [], chaos.report()
