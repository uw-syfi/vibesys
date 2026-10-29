"""Chaos on the skeleton search: generated fault plans and a seeded stop, through the shell.

The PR tier runs a small fixed set of seeds. ``CHAOS_SEED_RANGE=A-B`` (or a comma list) runs an explicit
range of seeds, ``CHAOS_LOG=path`` appends one JSON summary per run, and ``scripts/chaos_loop.sh N``
sweeps N seeds in parallel. A failure prints its seed, the injected faults, the violations and a
one-line repro.

Host crashes are not drawn here: the crash sweeps (``tests/system/crash``) own them. What a chaos
run checks is that typed failures of the agent and of the cluster, and an operator's stop, end the
run in a way those faults explain, leave no job behind and leave a record that passes the
liveness rules. Which search choices follow from a failure is for the search's own stories.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.loop_invariants import RunRecords, cluster_jobs
from tests.support.skeleton_strategy import SkeletonConfig
from tests.system.skeleton_chaos import (
    ROLES,
    ChaosInvariant,
    FaultyCluster,
    Injected,
    Ticks,
    UnresolvedDispatchError,
    plan_for,
    run_chaos,
    unexplained_end,
)

from vibesys.api import RunStatus, RunStopped
from vs_agent.api import AgentOutputSchemaError
from vs_agent.api.testing import AgentCrashError
from vs_faults.api import AgentFault, Boundary, ClusterFault, ClusterOperation, FaultPlan, FaultRule
from vs_runtime.api import RunCleanupError, RuntimeContractError
from vs_runtime.api.infrastructure import RuntimeRunControlChannel
from vs_sim.api.testing import Depth, SimThreads, depth, swept

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def _ignore(transition: object) -> None:
    del transition


# Each seed runs a whole skeleton search on real Git and the Fake cluster, so a pull request runs
# a few that cover a cluster fault, an agent crash, and a stop with two faults; the nightly
# workflow's depth draws twelve.
_PR_SEEDS = (2, 4, 9)
_NIGHTLY_SEEDS = tuple(range(12))


def _seeds() -> list[object]:
    spec = os.environ.get("CHAOS_SEED_RANGE")
    if spec is None:
        chosen = _NIGHTLY_SEEDS if depth() is Depth.NIGHTLY else _PR_SEEDS
        return [pytest.param(seed, id=f"seed_{seed}") for seed in chosen]
    seeds: list[object] = []
    for part in spec.split(","):
        low, _, high = part.partition("-")
        seeds.extend(
            pytest.param(seed, id=f"seed_{seed}") for seed in range(int(low), int(high or low) + 1)
        )
    return seeds


@pytest.mark.parametrize("seed", _seeds())
async def test_the_search_keeps_its_invariants_under_generated_faults(
    tmp_path: Path, seed: int
) -> None:
    chaos = await run_chaos(tmp_path, seed)

    assert chaos.violations == [], chaos.report()


async def test_a_run_without_faults_completes(tmp_path: Path) -> None:
    """With no fault and no stop the wrappers are the identity, so the run succeeds."""
    chaos = await run_chaos(tmp_path, 2, plan_for(2, faults=0), stop_at=None)

    assert chaos.injected == []
    assert chaos.ending.startswith("completed"), chaos.report()
    assert chaos.violations == [], chaos.report()


def _sampled[F](faults: Mapping[F, object], sample: int) -> list[F]:
    """The fault classes a pull request injects (spread over the table); the nightly job all."""
    found = list(faults)
    return [found[i] for i in swept(len(found), sample)]


# What each fault class does to a skeleton run: one explicit rule at its first opportunity, so
# the fault is certain to strike (the sweep above draws them at random).
_AGENT_ENDINGS = {
    AgentFault.CRASH: "failed",
    AgentFault.TIMEOUT: "failed",
    AgentFault.MALFORMED: "failed",
    AgentFault.SCHEMA_INVALID: "failed",
    # Every reply schema forbids unknown keys, so a reply with one is a schema error.
    AgentFault.EXTRA_KEYS: "failed",
}


@pytest.mark.parametrize("fault", _sampled(_AGENT_ENDINGS, 2), ids=lambda fault: fault.value)
async def test_an_agent_fault_is_injected_and_the_run_ends_as_that_fault_explains(
    tmp_path: Path, fault: AgentFault
) -> None:
    rule = FaultRule(boundary=Boundary.AGENT_TURN, target=ROLES[0], at=1, fault=fault)

    chaos = await run_chaos(
        tmp_path, 1, FaultPlan(seed=1, rules=(rule,)), stop_at=None, config=SkeletonConfig()
    )

    assert chaos.suffered.agent == {fault}, chaos.report()
    assert chaos.ending.startswith(_AGENT_ENDINGS[fault]), chaos.report()
    assert chaos.violations == [], chaos.report()


_CLUSTER_FAULTS = {
    # The first sbatch never reaches the cluster; core submits again under the same identity.
    ClusterFault.SSH_DOWN: ClusterOperation.SBATCH,
    ClusterFault.COMMAND_ERROR: ClusterOperation.SQUEUE,
    # The baseline's job is submitted and then dies, so there is no baseline to compare with.
    ClusterFault.KILLED: ClusterOperation.SBATCH,
    ClusterFault.WRONG_STATE: ClusterOperation.SACCT,
    # The baseline's job never leaves the queue; the queue deadline cancels it and it fails.
    ClusterFault.PENDING_FOREVER: ClusterOperation.SBATCH,
    ClusterFault.ACCOUNTING_GAP: ClusterOperation.SACCT,
    ClusterFault.REQUEUED: ClusterOperation.SQUEUE,
    ClusterFault.TRUNCATED: ClusterOperation.SBATCH,
}


@pytest.mark.parametrize("fault", _sampled(_CLUSTER_FAULTS, 3), ids=lambda fault: fault.value)
async def test_a_cluster_fault_is_injected_and_the_run_ends_as_that_fault_explains(
    tmp_path: Path, fault: ClusterFault
) -> None:
    rule = FaultRule(
        boundary=Boundary.CLUSTER, target=_CLUSTER_FAULTS[fault].value, at=1, fault=fault
    )

    chaos = await run_chaos(
        tmp_path, 1, FaultPlan(seed=1, rules=(rule,)), stop_at=None, config=SkeletonConfig()
    )

    assert chaos.suffered.cluster == {fault}, chaos.report()
    assert chaos.violations == [], chaos.report()
    if fault in {ClusterFault.KILLED, ClusterFault.PENDING_FOREVER}:
        assert chaos.ending.startswith("failed"), chaos.report()


async def test_a_stop_is_delivered_and_the_run_ends_stopped(tmp_path: Path) -> None:
    chaos = await run_chaos(tmp_path, 1, plan_for(1, faults=0), stop_at=3, config=SkeletonConfig())

    assert chaos.suffered.stop_delivered
    assert chaos.ending.startswith("stopped"), chaos.report()
    assert chaos.violations == [], chaos.report()


async def test_a_run_nothing_explains_failing_is_a_violation(tmp_path: Path) -> None:
    """The harness can fail: a run that ends failed with no fault injected is reported."""
    chaos = await run_chaos(
        tmp_path, 1, plan_for(1, faults=0), stop_at=None, config=SkeletonConfig(fails=True)
    )

    assert [kind for kind, _ in chaos.violations] == [ChaosInvariant.UNEXPLAINED_END]
    assert "reported failure without a fault" in chaos.report()


def test_a_job_the_run_never_saw_end_or_cancelled_is_a_violation() -> None:
    """The job ledger the shared invariant reads names a job left running."""
    cluster = FaultyCluster(
        plan_for(1, faults=0), Ticks(None, RuntimeRunControlChannel(_ignore, threads=SimThreads()))
    )
    cluster.submissions.extend(["seen", "cancelled", "left"])
    cluster.settled.add("seen")
    cluster.cancel_requested.add("cancelled")

    left = cluster_jobs(RunRecords(events=(), cluster_jobs=cluster.jobs()))

    assert [violation.detail for violation in left] == ["job left left RUNNING"]


# The crash case runs in every pull request; the other two reach the same bug through the same
# path and run with the slow tests.
@pytest.mark.parametrize(
    "fault",
    [
        AgentFault.CRASH,
        pytest.param(AgentFault.TIMEOUT, marks=pytest.mark.slow),
        pytest.param(AgentFault.MALFORMED, marks=pytest.mark.slow),
    ],
)
async def test_a_failed_peer_turn_does_not_stop_the_run_from_ending(
    tmp_path: Path, fault: AgentFault
) -> None:
    rule = FaultRule(boundary=Boundary.AGENT_TURN, target=ROLES[0], at=3, fault=fault)

    chaos = await run_chaos(
        tmp_path,
        1,
        FaultPlan(seed=1, rules=(rule,)),
        stop_at=None,
        config=SkeletonConfig(turns=1, peers=2),
    )

    assert chaos.violations == [], chaos.report()


@pytest.mark.slow  # about 25 s of virtual-clock stall detection
async def test_a_reply_naming_a_commit_that_does_not_exist_does_not_stop_the_run_from_ending(
    tmp_path: Path,
) -> None:
    rule = FaultRule(
        boundary=Boundary.AGENT_TURN, target=ROLES[0], at=1, fault=AgentFault.WRONG_VALUES
    )

    chaos = await run_chaos(
        tmp_path, 1, FaultPlan(seed=1, rules=(rule,)), stop_at=None, config=SkeletonConfig()
    )

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


@pytest.mark.parametrize("fault", list(ClusterFault))
def test_no_cluster_fault_explains_a_run_that_stopped_without_a_delivered_stop(
    fault: ClusterFault,
) -> None:
    """A deadlock that runs into the deadline must not pass because some cluster fault fired.

    Past its deadline a run cancels every job it still owns, so not even a fault that hides a
    job's fate (a lost reply, a job that never starts) can hold the run open until then.
    """
    verdict = unexplained_end(None, RunStatus.STOPPED, Injected(cluster=frozenset({fault})))

    assert verdict is not None


async def test_a_run_past_its_virtual_budget_is_stopped_and_reported_as_a_hang(
    tmp_path: Path,
) -> None:
    """A stuck run that keeps waiting is ended at the budget, so a hang fails in seconds.

    A budget shorter than any run stands in for a run that never ends.
    """
    chaos = await run_chaos(
        tmp_path, 1, FaultPlan(seed=1), stop_at=None, config=SkeletonConfig(), budget_s=2.0
    )

    assert [kind for kind, _ in chaos.violations] == [ChaosInvariant.HANG], chaos.report()
    assert "still waiting on" in chaos.violations[0][1]


@pytest.mark.slow  # about 20 s each: the run waits the lost job out to its deadline
@pytest.mark.parametrize("scancel", [3, 4])
@pytest.mark.parametrize("fault", [ClusterFault.SSH_DOWN, ClusterFault.COMMAND_ERROR])
async def test_a_measurement_whose_submit_and_cancel_both_fail_still_ends_the_run(
    tmp_path: Path, fault: ClusterFault, scancel: int
) -> None:
    """Seeds 268 and 479: the candidate's sbatch fails, then its close-time scancel fails too.

    Fixed in vs-core by #2344 and in the Slurm executor by #2328; vs-core's own tests pin
    each layer fast, and this is the end-to-end check.
    """
    rules = (
        FaultRule(
            boundary=Boundary.CLUSTER, target=ClusterOperation.SBATCH.value, at=2, fault=fault
        ),
        FaultRule(
            boundary=Boundary.CLUSTER,
            target=ClusterOperation.SCANCEL.value,
            at=scancel,
            fault=fault,
        ),
    )

    chaos = await run_chaos(
        tmp_path, 1, FaultPlan(seed=1, rules=rules), stop_at=None, config=SkeletonConfig()
    )

    assert chaos.violations == [], chaos.report()
