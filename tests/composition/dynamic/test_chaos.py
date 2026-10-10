"""Chaos sweep of the dynamic search on the core path: generated agents under seeded fault plans.

The PR tier runs a fixed set of seeds. ``CHAOS_SEEDS=A-B`` (or a comma list) runs other seeds;
``scripts/chaos_dynamic_loop.sh N`` sweeps N seeds in parallel. A failure prints its seed, the
injected faults, the violations, and a one-line repro.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.composition.dynamic._chaos import Injected, plan_for, run_chaos, unexplained_end

from vibesys.api import RunStatus, RunStopped
from vs_agent.api import AgentOutputSchemaError
from vs_agent.api.testing import AgentCrashError
from vs_faults.api import AgentFault, Boundary, ClusterFault
from vs_runtime.api import RunCleanupError, RuntimeContractError, UnresolvedDispatchError

if TYPE_CHECKING:
    from pathlib import Path

# Each seed runs a whole search (several seconds on a CI runner), so a pull request runs a few
# that cover no fault, an agent fault, and a cluster fault. The nightly workflow sweeps many more.
_PR_SEEDS = "0,1,3"


# A submission whose sbatch exec dies with the SSH link down has an unknown outcome, and the
# run then reconciles it forever instead of ending (seeds 2 and 4 of 0-14). The sweep keeps
# running such seeds so the gap stays measured; they cannot fail a pull request.
_SUBMISSION_GAP = pytest.mark.xfail(
    reason="an unknown-outcome submission is reconciled forever (#1776)", strict=False
)


def _declares_a_lost_submission(seed: int) -> bool:
    return any(
        rule.boundary is Boundary.CLUSTER and rule.fault is ClusterFault.SSH_DOWN
        for rule in plan_for(seed).rules
    )


def _seeds() -> list[object]:
    spec = os.environ.get("CHAOS_SEEDS", _PR_SEEDS)
    seeds: list[object] = []
    for part in spec.split(","):
        low, _, high = part.partition("-")
        seeds.extend(
            pytest.param(
                seed,
                id=f"seed_{seed}",
                marks=_SUBMISSION_GAP if _declares_a_lost_submission(seed) else (),
            )
            for seed in range(int(low), int(high or low) + 1)
        )
    return seeds


@pytest.mark.parametrize("seed", _seeds())
def test_the_search_keeps_its_invariants_under_generated_agents_and_faults(
    tmp_path: Path, seed: int
) -> None:
    chaos = run_chaos(tmp_path, seed)

    assert chaos.violations == [], chaos.report()


def test_a_run_without_faults_ends_without_an_error(tmp_path: Path) -> None:
    """With no fault scheduled, the fault wrappers are the identity, so no turn's fate is unknown."""
    chaos = run_chaos(tmp_path, 7000, plan_for(7000, faults=0))

    assert chaos.run is not None
    assert chaos.injected == []
    assert chaos.run.error is None, chaos.report()
    assert chaos.violations == [], chaos.report()


_UNRESOLVED = UnresolvedDispatchError("H-01: unresolved provider dispatch requires reconciliation")
_TRANSPORT = Injected(agent=frozenset({AgentFault.CRASH}))


@pytest.mark.parametrize(
    ("error", "status", "injected"),
    [
        (_UNRESOLVED, None, Injected()),
        (_UNRESOLVED, None, Injected(agent=frozenset({AgentFault.MALFORMED}))),
        (AgentCrashError("x"), None, Injected(agent=frozenset({AgentFault.TIMEOUT}))),
        (AgentOutputSchemaError("x"), None, _TRANSPORT),
        (RunStopped("x"), None, Injected()),
        (None, RunStatus.STOPPED, Injected()),
        (None, RunStatus.FAILED, Injected()),
        # The bug this suite once missed: a refused durable client is no fault's doing.
        (RuntimeContractError("durable client must implement AgentTurnExecutor"), None, _TRANSPORT),
        (RunCleanupError("cleanup", ()), None, _TRANSPORT),
    ],
)
def test_an_ending_no_injected_fault_explains_is_a_violation(
    error: BaseException | None, status: RunStatus | None, injected: Injected
) -> None:
    assert unexplained_end(error, status, injected) is not None


@pytest.mark.parametrize(
    ("error", "status", "injected"),
    [
        (None, RunStatus.COMPLETED, Injected()),
        (_UNRESOLVED, None, _TRANSPORT),
        (_UNRESOLVED, None, Injected(agent=frozenset({AgentFault.TIMEOUT}))),
        (AgentCrashError("x"), None, _TRANSPORT),
        (AgentOutputSchemaError("x"), None, Injected(agent=frozenset({AgentFault.SCHEMA_INVALID}))),
        (RunStopped("x"), None, Injected(stop_delivered=True)),
        (None, RunStatus.STOPPED, Injected(stop_delivered=True)),
        (None, RunStatus.FAILED, Injected(agent=frozenset({AgentFault.WRONG_VALUES}))),
        # Seed 2 on #1721: exec ssh_down plus sacct wrong_state left a cancellation's
        # outcome unknown, so cleanup ended the run with a typed error.
        (
            RunCleanupError("evaluation agent cleanup failed", ()),
            None,
            Injected(cluster=frozenset({ClusterFault.SSH_DOWN, ClusterFault.WRONG_STATE})),
        ),
    ],
)
def test_an_ending_an_injected_fault_explains_is_accepted(
    error: BaseException | None, status: RunStatus | None, injected: Injected
) -> None:
    assert unexplained_end(error, status, injected) is None


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

    verdict = unexplained_end(cleanup, None, Injected(cluster=declared))

    assert (verdict is None) == bool(declared)
    assert unexplained_end(cleanup, None, Injected(agent=frozenset(AgentFault))) is not None
