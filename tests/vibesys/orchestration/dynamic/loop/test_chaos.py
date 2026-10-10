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
from hypothesis import given
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
from vs_faults.api import AgentCrashError, AgentFault, Boundary, ClusterFault
from vs_runtime.api import RunCleanupError, RuntimeContractError, UnresolvedDispatchError

if TYPE_CHECKING:
    from pathlib import Path

# Seeds 2018 and 2022 completed with zero workstreams before #1228. Each seed runs
# a whole loop (10 to 20 s on a CI runner), so a pull request runs one generic
# faulted even seed beside those two; the fault-free run below covers seed 0's class.
# Odd seeds provision the profiler and are
# non-strict xfails (see _PROFILER_GAP): they cannot fail a pull request, so only
# the nightly workflow, which runs 0-11 and sweeps many more, spends time on them.
_PR_SEEDS = "2,2018,2022"


# Odd seeds provision the profiler. The generated profiler agent replies
# `waiting_for_evaluation` with handles it never obtained (the host rightly
# fails the profile), so `capability_unserved` fires on most of them. Scripting
# a profiler that submits its own capture and waits on the real handle made the
# run hang in the host instead (seeds 5, 15), so that gap is open: tracked as a
# follow-up, not exempted from the invariant.
_PROFILER_GAP = pytest.mark.xfail(
    reason="generated profiler agent has no real evaluation handles to cite", strict=False
)


def _seeds() -> list[object]:
    spec = os.environ.get("CHAOS_SEEDS", _PR_SEEDS)
    seeds: list[object] = []
    for part in spec.split(","):
        low, _, high = part.partition("-")
        seeds.extend(
            pytest.param(seed, id=f"seed_{seed}", marks=_PROFILER_GAP if seed % 2 else ())
            for seed in range(int(low), int(high or low) + 1)
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


def test_a_run_without_faults_ends_without_an_error(tmp_path: Path) -> None:
    """With no fault scheduled, the fault wrapper is the identity, so no turn's fate is unknown."""
    chaos = run_chaos(tmp_path, 7000, plan_for(7000, faults=0))

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
        # Seed 2 on #1721: exec ssh_down plus sacct wrong_state left a cancellation's
        # outcome unknown, so cleanup ended the run with a typed error.
        (
            RunCleanupError("evaluation agent cleanup failed", ()),
            Injected(cluster=frozenset({ClusterFault.SSH_DOWN, ClusterFault.WRONG_STATE})),
        ),
    ],
)
def test_an_ending_an_injected_fault_explains_is_accepted(
    error: BaseException | None, injected: Injected
) -> None:
    assert unexplained_end(error, injected) is None


@given(seed=st.integers(min_value=0, max_value=10_000))
def test_the_oracle_verdict_depends_on_the_faults_a_seed_declares_not_on_which_call_got_them(
    seed: int,
) -> None:
    """Whichever Slurm call receives a declared cluster fault, a cleanup ending is accepted."""
    plan = plan_for(seed)
    assert plan == plan_for(seed)
    declared = frozenset(
        ClusterFault(str(rule.fault)) for rule in plan.rules if rule.boundary is Boundary.CLUSTER
    )
    cleanup = RunCleanupError("evaluation agent cleanup failed", ())

    verdict = unexplained_end(cleanup, Injected(cluster=declared))

    assert (verdict is None) == bool(declared)
    assert unexplained_end(cleanup, Injected(agent=frozenset(AgentFault))) is not None
