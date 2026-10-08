"""Chaos sweep of the dynamic loop: generated agents under seeded fault plans.

The PR tier runs a fixed set of seeds. ``CHAOS_SEEDS=A-B`` (or a comma list)
runs other seeds; ``scripts/chaos_dynamic_loop.sh N`` sweeps N seeds in
parallel. A failure prints its seed, the injected faults, the violations, and
a one-line repro.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.loop._chaos import (
    Injected,
    plan_for,
    run_chaos,
    unexplained_end,
)

from vibesys.api import RunStatus, RunStopped
from vibesys.orchestration.dynamic import DynamicPlanningError
from vs_agent.api import AgentOutputSchemaError
from vs_faults.api import AgentCrashError, AgentFault
from vs_runtime.api import RunCleanupError, RuntimeContractError, UnresolvedDispatchError

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

    assert chaos.run is not None
    assert chaos.run.status is RunStatus.STOPPED
    assert chaos.run.error is None
    assert chaos.violations == [], chaos.report()


@settings(max_examples=3, deadline=None, suppress_health_check=list(HealthCheck))
@given(seed=st.integers(7000, 7003))
def test_a_run_without_faults_ends_without_an_error(seed: int) -> None:
    """With no fault scheduled, the fault wrapper is the identity, so no turn's fate is unknown."""
    with tempfile.TemporaryDirectory() as base:
        chaos = run_chaos(Path(base), seed, plan_for(seed, faults=0))

    assert chaos.run is not None
    assert chaos.injected == []
    assert chaos.run.error is None, chaos.report()
    assert chaos.violations == [], chaos.report()


_UNRESOLVED = UnresolvedDispatchError("H-01: unresolved provider dispatch requires reconciliation")
_TRANSPORT = Injected(agent=frozenset({AgentFault.CRASH}))


@pytest.mark.parametrize(
    ("error", "injected"),
    [
        (_UNRESOLVED, Injected()),
        (_UNRESOLVED, Injected(agent=frozenset({AgentFault.MALFORMED}))),
        (AgentCrashError("x"), Injected(agent=frozenset({AgentFault.TIMEOUT}))),
        (AgentOutputSchemaError("x"), _TRANSPORT),
        (DynamicPlanningError(None), Injected()),
        (DynamicPlanningError(None), _TRANSPORT),
        (RunStopped("x"), Injected()),
        # The bug this suite once missed: a refused durable client is no fault's doing.
        (RuntimeContractError("durable client must implement AgentTurnExecutor"), _TRANSPORT),
        (RunCleanupError("cleanup", ()), _TRANSPORT),
    ],
)
def test_an_ending_no_injected_fault_explains_is_a_violation(
    error: BaseException, injected: Injected
) -> None:
    assert unexplained_end(error, injected) is not None


@pytest.mark.parametrize(
    ("error", "injected"),
    [
        (None, Injected()),
        (_UNRESOLVED, _TRANSPORT),
        (_UNRESOLVED, Injected(agent=frozenset({AgentFault.TIMEOUT}))),
        (AgentCrashError("x"), _TRANSPORT),
        (AgentOutputSchemaError("x"), Injected(agent=frozenset({AgentFault.SCHEMA_INVALID}))),
        (DynamicPlanningError(None), Injected(agent=frozenset({AgentFault.WRONG_VALUES}))),
        (RunStopped("x"), Injected(stop_delivered=True)),
    ],
)
def test_an_ending_an_injected_fault_explains_is_accepted(
    error: BaseException | None, injected: Injected
) -> None:
    assert unexplained_end(error, injected) is None
