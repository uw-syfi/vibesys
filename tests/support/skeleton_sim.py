"""Deterministic simulation of the skeleton run under a host-fault plan.

``simulate`` plays the scripted run on the composed world. Each time the plan crashes the host
(a ``HostCrashError`` from the faulting executors or the faulting store), the clock moves past
the dead host's lease and a fresh shell starts over the same Project, the same Fake cluster and
the same agent journal, exactly as a restart would. Once the plan is spent the last host runs
fault-free to the end (heal, then liveness). The ``Summary`` is what must not depend on where the
host died: the terminal result, the adopted tree, and the external effects by idempotency key.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.support.runtime_evaluation import ScenarioCluster
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import (
    LEASE,
    CandidateResolver,
    StalledError,
    drive,
    open_skeleton_world,
)

from vs_core.api import IntentPhase, RunStatus
from vs_faults.api import FaultGate, FaultPlan, HostCrashError
from vs_project.api import run_git

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.skeleton_world import Process, World

    from vs_core.api import CoreState, RequestId
    from vs_slurm.api import ClusterSubmitOutcome, SlurmBatchRequest, SlurmJobRequest


class CountingCluster(ScenarioCluster):
    """The Fake cluster, counting every sbatch call by its idempotency key."""

    def __init__(self) -> None:
        """Start with no calls."""
        super().__init__()
        self.sbatch_calls: Counter[str] = Counter()

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        """Count the call, then behave as the scripted Fake."""
        self.sbatch_calls[operation_id] += 1
        return super().submit(request, operation_id=operation_id)


@dataclass(frozen=True)
class Summary:
    """What a run leaves behind, independent of where the host died."""

    outcome: str | None
    status: RunStatus
    adopted_tree: str | None
    sbatch_calls: tuple[int, ...]
    """Calls per idempotency key. Keys embed commit hashes, which carry timestamps."""
    agent_dispatches: tuple[tuple[str, int], ...]
    crashes: int
    stalled: str | None = None
    """Why the run could not leave its last host, when it stalled instead of ending."""


@dataclass(frozen=True)
class Simulation:
    """A finished simulated run, its summary and the gate that counted its boundary crossings."""

    summary: Summary
    gate: FaultGate
    core: CoreState


async def simulate(
    root: Path, plan: FaultPlan, strategy: SkeletonStrategy | None = None
) -> Simulation:
    """Run to a terminal state through every crash ``plan`` schedules, restarting each time."""
    gate = FaultGate(plan)
    cluster = CountingCluster()
    with open_skeleton_world(root, strategy or SkeletonStrategy(), cluster, gate=gate) as world:
        now = 0.0
        crashes = 0
        for _ in range(len(plan.rules) + 1):
            process = world.runtime()
            try:
                process.shell.start(f"host-{crashes}", now_at=now, lease_duration=LEASE)
                refusal = await drive(process, start=now + 1.0)
            except StalledError as stalled:
                return Simulation(
                    _summary(world, cluster, process, crashes, f"{stalled}; {_waiting(process)}"),
                    gate,
                    process.shell.record.envelope.core,
                )
            except HostCrashError:
                crashes += 1
                now += LEASE + 2.0
                continue
            assert refusal is None, refusal
            summary = _summary(world, cluster, process, crashes, None)
            return Simulation(summary, gate, process.shell.record.envelope.core)
    message = f"the host kept crashing past its plan {plan.model_dump_json()}"
    raise AssertionError(message)


def _summary(
    world: World, cluster: CountingCluster, process: Process, crashes: int, stalled: str | None
) -> Summary:
    core = process.shell.record.envelope.core
    resolver = world.agents.resolver
    assert isinstance(resolver, CandidateResolver)
    assert resolver.writer is not None
    dispatches = Counter(str(key) for key in resolver.writer.invocations)
    return Summary(
        outcome=None if core.run.result is None else core.run.result.outcome,
        status=core.run.status,
        adopted_tree=_adopted_tree(world.root, core),
        sbatch_calls=tuple(sorted(cluster.sbatch_calls.values())),
        agent_dispatches=tuple(sorted(dispatches.items())),
        crashes=crashes,
        stalled=stalled,
    )


def _waiting(process: Process) -> str:
    """What the stalled run still waits for: unresolved recovery checks and unended requests."""
    core = process.shell.record.envelope.core
    recovery = core.intents.recovery
    rows = {intent.request_id: intent for intent in core.intents.intents}

    def show(request_id: RequestId | None) -> str:
        intent = None if request_id is None else rows.get(request_id)
        if intent is None:
            return "none"
        seen = intent.observation
        facts = (
            "no observation" if seen is None else f"{seen.status.value} terminal={seen.terminal}"
        )
        return f"{intent.request.kind} {intent.phase.value} ({facts})"

    pending = [
        f"{show(c.target)} inspected by {show(c.inspection)}"
        for c in recovery.checks
        if c.resolution in ("pending", "blocked")
    ]
    unended = [show(i.request_id) for i in rows.values() if i.phase != IntentPhase.COMPLETED]
    return (
        f"recovery {recovery.phase.value} epoch {recovery.epoch}; pending checks {pending}; "
        f"unended requests {unended}"
    )


def _adopted_tree(root: Path, core: CoreState) -> str | None:
    """The tree of the adopted revision: commit hashes carry timestamps, trees do not."""
    adoption = core.settlement.adoption
    if adoption is None or adoption.observation is None or adoption.observation.revision is None:
        return None
    commit = adoption.observation.revision.revision_id.root
    result = run_git(["rev-parse", f"{commit}^{{tree}}"], cwd=root)
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


__all__ = ["CountingCluster", "Simulation", "Summary", "simulate"]
