"""One production-shaped core run, built the way a launch will build it.

``open_skeleton_world`` gives a real Git project with durable state under a
temporary Project, the real workspace and evaluation executors (evaluation runs
on the Fake Slurm cluster), and the scripted strategy. ``World.runtime`` builds a
new shell over that same disk, so a test can drop one shell mid-run and start
another, as after a process crash. Nothing here replaces core logic or patches
a module: a missing piece shows up as the real interface failing.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from tests.support.runtime_evaluation import ScenarioCluster, stage_failure_text
from tests.support.session_world import Reply, SessionHost, open_host
from tests.support.skeleton_strategy import DECLARATION, SkeletonState, SkeletonStrategy
from tests.support.workspace_world import RUN_ID, WorkspaceEnv, open_workspace_env

from vs_core.api import (
    ClockAdvanced,
    IntentPhase,
    Limits,
    RecoveryPhase,
    RoleId,
    RunFacts,
    RunStatus,
    SchemaRef,
)
from vs_runtime.api.core import (
    CoreRuntime,
    CoreRuntimeBindings,
    DispatchProgress,
    ExecutorRefusal,
    JournalPublicationDelivery,
    SessionServices,
    core_bindings,
    new_core_state,
    revision_ref,
)
from vs_runtime.api.infrastructure import (
    ScalarBenchmarkContract,
    SemanticSlurmEvaluationExecutor,
    TrustedEvaluationPlan,
)
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_slurm.api import SlurmConfig, SlurmSshTransport

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_core.api import CoreState

DIGEST = "ab" * 32
LEASE = 100.0


@dataclass
class World:
    """The disk state of one run and every shell started over it."""

    env: WorkspaceEnv
    root: Path
    cluster: ScenarioCluster
    agents: SessionHost
    measured: bool = True

    def initial(self) -> CoreState:
        """The state of a run that has not started, from the real baseline commit."""
        host = self.env.hosts[0]
        commit = host.root.revision
        assert commit is not None
        facts = RunFacts(
            objective="make candidate.py faster",
            baseline=revision_ref(commit),
            evaluator_digest=DIGEST,
            workload_digest=DIGEST,
            environment_digest=DIGEST,
        )
        return new_core_state(RUN_ID, facts, DECLARATION, deadline_at=1000.0, limits=Limits())

    def bindings(self) -> CoreRuntimeBindings:
        """Executors for one host process: new workspaces and evaluation over the shared disk."""
        workspaces = self.env.start_host()
        namespace = self.env.project.state.local_namespace(RUN_ID, "evaluation")
        evaluation = SemanticSlurmEvaluationExecutor(
            SlurmConfig(
                name="test",
                remote_workspace_root="/runs",
                transport=SlurmSshTransport(host="test"),
            ),
            SlurmExecutionPolicy(),
            SlurmEvaluationPlan(
                config_path=self.root / "slurm.toml",
                accuracy_command=("python", "accuracy.py"),
                benchmark_command=("python", "benchmark.py"),
            ),
            TrustedEvaluationPlan(
                accuracy_command="unused",
                benchmark_command="unused",
                benchmark_contract=ScalarBenchmarkContract(
                    output_argument="--output", metric="throughput"
                ),
            ),
            workspaces,
            namespace,  # type: ignore[arg-type]  # the evaluation namespace is a StateNamespace
            self.root / "handles",
            stage_failure_text=stage_failure_text,
            cluster=self.cluster,
        )
        return core_bindings(
            receipts=self.env.receipts_namespace(),
            workspaces=workspaces,
            evaluation=evaluation,
            sessions=SessionServices(self.agents.sessions(), self.agents.resolver),
        )

    def _strategy(self) -> SkeletonStrategy:
        return SkeletonStrategy() if self.measured else SkeletonStrategy.unmeasured()

    def runtime(self) -> Process:
        """A shell over this run's durable store, as one new process would start it.

        A fresh run when the store is empty, a recovery of the durable envelope otherwise.
        """
        bindings = self.bindings()
        store = self.env.project.state_store(RUN_ID)
        shell: CoreRuntime[SkeletonState] = CoreRuntime(
            store, self._strategy(), self.initial(), bindings=bindings
        )
        delivery = JournalPublicationDelivery(
            self.env.project.state.portable_namespace(RUN_ID, "publications"),
            bindings.registry,
            store,
        )
        return Process(shell, delivery)


@dataclass
class Process:
    """One host process: a shell and the publication delivery it runs with."""

    shell: CoreRuntime[SkeletonState]
    delivery: JournalPublicationDelivery


def finished(process: Process) -> bool:
    """Whether the run reached its terminal status."""
    return process.shell.record.envelope.core.run.status == RunStatus.TERMINAL


def _recovered(process: Process) -> bool:
    """Whether core finished reconciling unfinished work, so the strategy may be asked."""
    barrier = process.shell.record.envelope.core.intents.recovery
    return barrier.phase == RecoveryPhase.READY


class StalledError(AssertionError):
    """The run is not terminal, nothing is dispatchable and the strategy has nothing to add."""


async def drive(process: Process, *, start: float, rounds: int = 40) -> ExecutorRefusal | None:
    """Deliver the clock, ask the strategy once recovered, and run to idle, until terminal.

    Time is a logical counter the caller supplies; no sleeps and no wall clock.
    Raises ``StalledError`` when a round changes nothing, which names a request
    nobody issues rather than burning the remaining rounds.
    """
    now = start
    for _ in range(rounds):
        if finished(process):
            return None
        before = process.shell.record.envelope.core.revision
        process.shell.submit(ClockAdvanced(now_at=now), now_at=now)
        refusal = await process.shell.run_until_idle(process.delivery, now_at=now)
        if refusal is None and _recovered(process):
            process.shell.decide(now_at=now)
            refusal = await process.shell.run_until_idle(process.delivery, now_at=now)
        if refusal is not None:
            return refusal
        if _stalled(process, before):
            raise StalledError(_describe(process))
        now += 1.0
    message = f"run did not finish in {rounds} rounds"
    raise AssertionError(message)


def _stalled(process: Process, before: int) -> bool:
    core = process.shell.record.envelope.core
    return core.revision - before <= 2 and not any(
        intent.phase == IntentPhase.PREPARED for intent in core.intents.intents
    )


def _describe(process: Process) -> str:
    core = process.shell.record.envelope.core
    open_intents = [
        f"{intent.request.kind}:{intent.phase.value}"
        for intent in core.intents.intents
        if intent.phase != IntentPhase.COMPLETED
    ]
    return f"stalled at core revision {core.revision}; open intents {open_intents}"


class CrashPoint(StrEnum):
    """Where the scenario drops its shell, as a process crash would."""

    AFTER_DISPATCH = "after-dispatch"
    AFTER_OBSERVATION = "after-observation"


async def run_until_crash(process: Process, point: CrashPoint, *, start: float) -> float:
    """Drive to the first executed request, then stop where ``point`` says and drop the shell.

    After dispatch: the executor ran, its observation is not yet committed.
    After observation: the observation is committed, nothing after it is.
    Returns the logical time of the crash. The caller starts another process.
    """
    now = start
    shell = process.shell
    for _ in range(40):
        shell.submit(ClockAdvanced(now_at=now), now_at=now)
        shell.decide(now_at=now)
        while shell.advance():
            pass
        outcome = await shell.dispatch_one(now_at=now)
        if outcome == DispatchProgress.DISPATCHED:
            if point == CrashPoint.AFTER_OBSERVATION:
                assert shell.advance()
            return now
        if outcome != DispatchProgress.IDLE:
            message = f"refused before the crash point: {outcome}"
            raise RuntimeError(message)
        now += 1.0
    message = "no request was dispatched"
    raise AssertionError(message)


@contextmanager
def open_skeleton_world(tmp_path: Path, *, measured: bool = True) -> Iterator[World]:
    """A fresh run on real Git with the Fake Slurm cluster. Closes every host on exit."""
    with open_workspace_env(tmp_path) as env:
        agents = open_host(tmp_path / "project")
        agents.resolver.roles = frozenset({RoleId(root="implementer")})
        agents.resolver.schemas = {SchemaRef(name="implementation", version=1): Reply}
        yield World(
            env=env,
            root=tmp_path / "project",
            cluster=ScenarioCluster(),
            agents=agents,
            measured=measured,
        )
