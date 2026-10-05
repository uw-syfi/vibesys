"""One production-shaped core run, built the way a launch will build it.

``open_skeleton_world`` gives a real Git project with durable state under a
temporary Project, the real workspace and evaluation executors (evaluation runs
on the Fake Slurm cluster), and the scripted strategy. ``World.runtime`` builds a
new shell over that same disk, so a test can drop one shell mid-run and start
another, as after a process crash. Nothing here replaces core logic or patches
a module: a missing piece shows up as the real interface failing.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from tests.support.fake_run_clock import FakeRunClock
from tests.support.runtime_evaluation import ScenarioCluster
from tests.support.session_world import (
    FakeSessionResolver,
    ProviderFaults,
    SessionHost,
)
from tests.support.skeleton_strategy import DECLARATION, SkeletonState, SkeletonStrategy
from tests.support.workspace_world import RUN_ID, WorkspaceEnv, open_workspace_env

from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeAgentInvocationStore, FakeDriver
from vs_core.api import (
    ClockAdvanced,
    RoleId,
    RunFacts,
    RunStatus,
    SchemaRef,
    TurnSpec,
)
from vs_project.api import run_git
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import render_stage_failure
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    DispatchProgress,
    ExecutorRefusal,
    JournalPublicationDelivery,
    OperationCatalog,
    RunLoopConfig,
    RunStalledError,
    SessionServices,
    core_bindings,
    drive_core,
    empty_catalog,
    new_core_state,
    revision_ref,
)
from vs_runtime.api.infrastructure import (
    ScalarBenchmarkContract,
    SemanticSlurmEvaluationExecutor,
    TrustedEvaluationPlan,
)
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_slurm.api import (
    ClusterInspectOutcome,
    ClusterObservation,
    ClusterSubmitOutcome,
    ClusterTarget,
    SlurmBatchRequest,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_agent.api import AgentSessionSpec, AgentTurnRequest
    from vs_core.api import CoreState
    from vs_runtime.api.core import AccessGuardedWorkspace

DIGEST = "ab" * 32
LEASE = 100.0


class TimedCluster(ScenarioCluster):
    """A Fake cluster whose jobs run for ``runtime`` seconds of the shared run clock.

    Each job reports RUNNING until the clock has moved ``runtime`` past its submission,
    then reports its scripted ending. ``inspections`` counts every scheduler observation,
    which is what a real cluster would charge as scheduler calls.
    """

    def __init__(self, clock: FakeRunClock, runtime: float) -> None:
        """Run every job for *runtime* seconds from the moment the scheduler accepts it."""
        super().__init__()
        self._clock = clock
        self._runtime = runtime
        self._accepted_at: dict[str, float] = {}
        self.inspections = 0

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        """Record when the job was first accepted, then behave as the scenario cluster."""
        self._accepted_at.setdefault(operation_id, self._clock.now())
        return super().submit(request, operation_id=operation_id)

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        """Observe the job, reporting RUNNING while its runtime has not yet elapsed."""
        outcome = super().inspect(target, by_job_id=by_job_id)
        if not isinstance(outcome, ClusterObservation):
            return outcome
        self.inspections += 1
        accepted = self._accepted_at.get(outcome.operation_id or "")
        if accepted is None or self._clock.now() >= accepted + self._runtime:
            return outcome
        return outcome.model_copy(update={"status": SlurmJobStatus.RUNNING})


@dataclass
class World:
    """The disk state of one run and every shell started over it."""

    env: WorkspaceEnv
    root: Path
    cluster: ScenarioCluster
    agents: SessionHost
    strategy: SkeletonStrategy = field(default_factory=SkeletonStrategy)
    operations: OperationCatalog = field(default_factory=empty_catalog)

    def initial(self) -> CoreState:
        """The state of a run that has not started, from the real baseline commit."""
        host = self.env.hosts[0]
        # The run's baseline is the trusted input baseline: that is what adoption of the
        # baseline is checked against.
        commit = host.root.trusted_input_baseline
        assert commit is not None
        facts = RunFacts(
            objective="make candidate.py faster",
            baseline=revision_ref(commit),
            evaluator_digest=DIGEST,
            workload_digest=DIGEST,
            environment_digest=DIGEST,
        )
        return new_core_state(
            RUN_ID, facts, DECLARATION, offered=self.operations, deadline_at=1000.0
        )

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
            stage_failure_text=render_stage_failure,
            cluster=self.cluster,
        )
        return core_bindings(
            receipts=self.env.receipts_namespace(),
            workspaces=workspaces,
            evaluation=evaluation,
            sessions=SessionServices(self.agents.sessions(), self.agents.resolver),
            operations=self.operations,
        )

    def runtime(self) -> Process:
        """A shell over this run's durable store, as one new process would start it.

        A fresh run when the store is empty, a recovery of the durable envelope otherwise.
        """
        bindings = self.bindings()
        store = self.env.project.state_store(RUN_ID)
        shell: CoreRuntime[SkeletonState] = CoreRuntime(
            store, self.strategy, self.initial(), bindings=bindings
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


class StalledError(AssertionError):
    """The run is not terminal, nothing is dispatchable and the strategy has nothing to add."""


async def drive(
    process: Process, *, start: float, clock: FakeRunClock | None = None
) -> ExecutorRefusal | None:
    """Run the production loop to a terminal run, on a fake clock that starts at ``start``.

    No sleeps and no wall clock. A stall (nothing to do and nothing time can wake)
    surfaces as ``StalledError``; a request cycle that never goes idle fails the
    loop's dispatch cap.
    """
    host = CoreRunHost(process.shell, process.delivery, clock or FakeRunClock(start))
    config = RunLoopConfig(
        host_id="skeleton",
        lease_duration=LEASE,
        recovery_poll_interval=50.0,
        max_dispatches=60,
    )
    try:
        outcome = await drive_core(host, config)
    except RunStalledError as error:
        raise StalledError(str(error)) from error
    return outcome.refusal


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


class Implementation(BaseModel):
    """The implementer's structured reply: the commit it made in its workspace."""

    commit: str


IMPLEMENTATION = SchemaRef(name="implementation", version=1)
IMPLEMENTER = RoleId(root="implementer")


@dataclass
class CandidateWriter:
    """The Fake provider's implementer: each turn commits one change in its candidate worktree.

    The worktree is found through Git (the one in an attempt member directory), so
    nothing here reaches into the executors. The reply names the new commit.
    """

    root: Path
    answer: dict[str, object] = field(default_factory=lambda: {"commit": ""})
    turns: int = 0

    def worktree(self) -> Path:
        """The path of the run's single candidate worktree."""
        listing = self._git(self.root, "worktree", "list", "--porcelain")
        paths = [
            Path(line.removeprefix("worktree "))
            for line in listing.splitlines()
            if line.startswith("worktree ")
        ]
        # An attempt's candidate lives in a member directory ``m-attempt-<id>``; the
        # evaluation executor keeps its own measurement worktrees (``s<id>``) beside it.
        candidates = [path for path in paths if path.parent.name.startswith("m-attempt-")]
        # A probe that runs a turn with no attempt has only the root checkout to write in.
        candidates = candidates or [self.root]
        assert len(candidates) == 1, listing
        return candidates[0]

    def __call__(self, request: AgentTurnRequest) -> None:
        """Write and commit one change, then name the commit in the reply."""
        del request
        self.turns += 1
        tree = self.worktree()
        (tree / "candidate.py").write_text(f"VALUE = {self.turns + 1}\n", encoding="utf-8")
        self._git(tree, "add", "-A")
        self._git(tree, "commit", "-m", f"implement {self.turns}")
        self.answer["commit"] = self._git(tree, "rev-parse", "HEAD").strip()

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        identity = ["-c", "user.name=agent", "-c", "user.email=agent@example.com"]
        result = run_git([*identity, *args], cwd=cwd)
        assert result.returncode == 0, result.stderr
        return result.stdout.decode()


@dataclass
class CandidateResolver(FakeSessionResolver):
    """Resolves turns to the candidate worktree the writer commits in."""

    writer: CandidateWriter | None = None

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The Fake provider's session configuration over the live candidate worktree."""
        assert self.writer is not None
        spec = super().agent_spec(turn, workspace)
        assert spec is not None
        return dataclasses.replace(spec, workspace=self.writer.worktree())


def _open_agents(root: Path) -> SessionHost:
    writer = CandidateWriter(root)
    client = AgentClient(FakeDriver(answer=writer.answer, on_turn=writer))
    resolver = CandidateResolver(
        root,
        TemplateRenderer(root),
        roles=frozenset({IMPLEMENTER}),
        schemas={IMPLEMENTATION: Implementation},
        writer=writer,
    )
    return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())


@contextmanager
def open_skeleton_world(
    tmp_path: Path,
    strategy: SkeletonStrategy | None = None,
    cluster: ScenarioCluster | None = None,
) -> Iterator[World]:
    """A fresh run on real Git with the Fake Slurm cluster. Closes every host on exit."""
    with open_workspace_env(tmp_path) as env:
        agents = _open_agents(tmp_path / "project")
        yield World(
            env=env,
            root=tmp_path / "project",
            cluster=cluster or ScenarioCluster(),
            agents=agents,
            strategy=strategy or SkeletonStrategy(),
        )
