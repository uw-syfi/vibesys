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
from typing import TYPE_CHECKING, cast

from tests.support.real_git import REAL_GIT
from tests.support.session_world import (
    FakeSessionResolver,
    ProviderFaults,
    SessionHost,
)
from tests.support.skeleton_faults import FaultingExecutors, FaultingReceipts, FaultingStore
from tests.support.skeleton_strategy import (
    DIGEST,
    IMPLEMENTATION,
    IMPLEMENTER,
    OPERATIONS,
    Implementation,
    SkeletonStrategy,
)
from tests.support.workspace_world import (
    RUN_ID,
    ExecutionTopology,
    WorkspaceEnv,
    open_workspace_env,
)

from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider
from vs_core.api import (
    ClockAdvanced,
    FlowState,
    Limits,
    RunFacts,
    RunStatus,
    TurnSpec,
)
from vs_core.api.testing import sample_limits
from vs_evaluation.api import ExecutorPoll, PollPhase
from vs_project.api.testing import CliWorldGit, GitKind, WorldGit, world_git
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, render_stage_failure
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    CoreStartup,
    DispatchProgress,
    ExecutorRefusal,
    JournalPublicationDelivery,
    MeasurementServices,
    NotApplied,
    OperationCatalog,
    OperationRole,
    RenderArtifactsOwner,
    RunLoopConfig,
    RunStalledError,
    SessionServices,
    build_operation_catalog,
    core_bindings,
    drive_core,
    empty_catalog,
    new_core_state,
    operation_roles,
    revision_ref,
)
from vs_runtime.api.infrastructure import (
    ScalarBenchmarkContract,
    SemanticSlurmEvaluationExecutor,
    TrustedEvaluationPlan,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_sim.api.testing import NO_PROCESSES, ManualClock, clock_from, current_virtual_clock
from vs_slurm.api import (
    SlurmConfig,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from vs_agent.api import AgentSessionSpec, AgentTurnRequest
    from vs_core.api import CoreState, OperationRequest
    from vs_evaluation.api import (
        AvailabilitySnapshot,
        EvaluationRequest,
        ExecutorObservation,
        PollingEvaluationExecutor,
        ResourceRequirements,
    )
    from vs_faults.api import FaultGate
    from vs_project.api import StateNamespace, StateStore
    from vs_runtime.api.core import (
        AccessGuardedWorkspace,
        CommitObserver,
        ExecutionContext,
        Inspection,
        TurnYields,
    )
    from vs_runtime.api.infrastructure import RuntimeWorkspaces
    from vs_sim.api import Clock, SleepingClock, Threads

LEASE = 100.0
# A Slurm job that has not ended this long after it was submitted is cancelled and fails: well
# inside the run deadline (1000 s), so a job that never leaves the queue cannot outlive the run.
JOB_TIMEOUT_S = 300


@dataclass
class TimedPolls:
    """The world's evaluation executor, except that each job runs ``runtime`` clock seconds.

    A job reports RUNNING until the shared run clock has moved ``runtime`` past the first
    poll that saw it submitted; then it reports what the real executor reports. Everything
    else is the real executor. ``polls`` is one entry per poll core asked for, which is what
    pacing bounds.
    """

    inner: PollingEvaluationExecutor
    clock: Clock
    runtime: float
    polls: list[str]
    during_submit: Callable[[], Awaitable[None]] | None = None
    """Work that takes place while a submission is in flight, such as the run clock passing."""
    _seen: dict[str, float] = field(default_factory=dict)

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """The real executor's availability."""
        return await self.inner.availability(requirements)

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """The real submission, taking as long as ``during_submit`` lets it."""
        if self.during_submit is not None:
            await self.during_submit()
        await self.inner.submit(request, handle_id=handle_id)

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """The real read."""
        return await self.inner.inspect_only(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """The real read."""
        return await self.inner.inspect(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """The real wait."""
        await self.inner.wait_for_change(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> None:
        """The real cancellation."""
        await self.inner.cancel(handle_id)

    async def poll(self, handle_id: str) -> ExecutorPoll:
        """Report RUNNING while the job's runtime has not elapsed on the run clock."""
        self.polls.append(handle_id)
        polled = await self.inner.poll(handle_id)
        if polled.phase is PollPhase.UNSUBMITTED:
            return polled
        seen = self._seen.setdefault(handle_id, self.clock.now())
        if self.clock.now() >= seen + self.runtime:
            return polled
        return ExecutorPoll(phase=PollPhase.RUNNING, current_stage=polled.current_stage)


class _LoopPacing:
    """Paces job polls on the virtual loop the world runs on, so polling lets run time pass."""

    async def sleep(self, seconds: float) -> None:
        """Sleep on the running virtual loop's clock."""
        await current_virtual_clock().sleep(seconds)


@dataclass
class World:
    """The disk state of one run and every shell started over it."""

    env: WorkspaceEnv
    root: Path
    cluster: ScenarioCluster
    agents: SessionHost
    strategy: SkeletonStrategy = field(default_factory=SkeletonStrategy)
    operations: OperationCatalog = field(default_factory=empty_catalog)
    """What the host offers the search: the skeleton's operations, rendered from ``templates``."""
    timed: tuple[Clock, float] | None = None
    job_clock: Clock = field(default_factory=ManualClock)
    """What Slurm job deadlines count on: by default a clock that never moves, so none expires.
    A world on a virtual loop hands its run clock, so a job outliving ``JOB_TIMEOUT_S`` fails."""
    polls: list[str] = field(default_factory=list)
    during_submit: Callable[[], Awaitable[None]] | None = None
    limits: Limits = field(default_factory=lambda: sample_limits(reconciliation_bound=300.0))
    """A reconciliation bound that outlasts the restart delay of the crash sweeps (one lease and a
    little more). Core does not yet keep a restart later than its own bounds: a poll of an owned
    job that lapsed while the host was down fails the measurement or orphans the job instead of
    being polled anew (`vs_core._waits`, OWNED_JOB halts the run). Use the default limits once
    core keeps that property."""
    yields: Callable[[RuntimeWorkspaces, StateNamespace], TurnYields] | None = None
    """Builds the turn-yield source from the host's workspaces and receipts, per process."""
    commits: CommitObserver | None = None
    """Told of every confirmed commit of each shell this world starts."""
    gate: FaultGate | None = None
    git: WorldGit | None = None
    """The Git the world runs on, as the scripted agent and the assertions see it."""
    store: StateStore | None = None
    """The run's state store, shared by every host the world starts as the disk is; None opens
    the project's store on disk. A memory store spares a test that restarts the host many
    times the store's file writes and directory syncs."""
    evaluations: list[SemanticSlurmEvaluationExecutor] = field(default_factory=list)
    """The evaluation executor of every process started, which a host closes at exit."""

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
            RUN_ID,
            facts,
            self.strategy.declaration,
            offered=self.operations,
            startup=CoreStartup(
                deadline_at=1000.0,
                limits=self.limits,
                # Waiting for evaluations is served exactly when the world has a yield source.
                lifecycle=frozenset() if self.yields is None else frozenset({"suspend"}),
            ),
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
                job_timeout_seconds=JOB_TIMEOUT_S,
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
            clock=self.job_clock,
            sleeper=_LoopPacing(),
            cluster=self.cluster,
            blocking=self.env.execution.blocking,
            processes=NO_PROCESSES,
        )
        self.evaluations.append(evaluation)
        if self.timed is not None:
            clock, runtime = self.timed
            evaluation = TimedPolls(evaluation, clock, runtime, self.polls, self.during_submit)
        receipts = self.env.receipts_namespace()
        if self.gate is not None:
            # The gated namespace delegates all but the execution-record writes to the real one.
            receipts = cast("StateNamespace", FaultingReceipts(receipts, self.gate))
        bindings = core_bindings(
            receipts=receipts,
            workspaces=workspaces,
            measurement=MeasurementServices(evaluation),
            sessions=SessionServices(
                self.agents.sessions(),
                self.agents.resolver,
                None
                if self.yields is None
                else self.yields(workspaces, self.env.receipts_namespace()),
            ),
            operations=self.operations,
            threads=self.env.execution.threads,
            blocking=self.env.execution.blocking,
        )
        # The skeleton checks for orphan waits after every commit, so a stall fails where it starts.
        bindings = dataclasses.replace(bindings, check_liveness=True)
        if self.gate is None:
            return bindings
        return dataclasses.replace(
            bindings, executors=FaultingExecutors.around(bindings.executors, self.gate)
        )

    def runtime(self) -> Process:
        """A shell over this run's durable store, as one new process would start it.

        A fresh run when the store is empty, a recovery of the durable envelope otherwise.
        """
        bindings = self.bindings()
        if self.commits is not None:
            bindings = dataclasses.replace(bindings, commits=self.commits)
        store = self.store if self.store is not None else self.env.project.state_store(RUN_ID)
        if self.gate is not None:
            store = FaultingStore(store, self.gate)
        shell = CoreRuntime(
            store,
            self.strategy.driver(bindings.registry),
            self.initial(),
            config=self.strategy.config,
            bindings=bindings,
        )
        delivery = JournalPublicationDelivery(
            self.env.project.state.portable_namespace(RUN_ID, "publications"),
            bindings.registry,
        )
        return Process(shell, delivery)


@dataclass
class Process:
    """One host process: a shell and the publication delivery it runs with."""

    shell: CoreRuntime[FlowState]
    delivery: JournalPublicationDelivery


def finished(process: Process) -> bool:
    """Whether the run reached its terminal status."""
    return process.shell.record.envelope.core.run.status == RunStatus.TERMINAL


class StalledError(AssertionError):
    """The run is not terminal, nothing is dispatchable and the strategy has nothing to add."""


async def drive(
    process: Process, *, start: float, clock: SleepingClock | None = None
) -> ExecutorRefusal | None:
    """Run the production loop to a terminal run, on a fake clock that starts at ``start``.

    No sleeps and no wall clock. A stall (nothing to do and nothing time can wake)
    surfaces as ``StalledError``; a request cycle that never goes idle fails the
    loop's dispatch cap.
    """
    host = CoreRunHost(process.shell, process.delivery, clock or clock_from(start))
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


@dataclass
class CandidateWriter:
    """The Fake provider's implementer: each turn commits one change in its candidate worktree.

    The worktree is found through Git (the one in an attempt member directory), so
    nothing here reaches into the executors. The reply names the new commit.
    """

    root: Path
    answer: dict[str, object] = field(default_factory=lambda: {"commit": ""})
    turns: int = 0
    invocations: list[str | None] = field(default_factory=list)
    vcs: WorldGit = field(init=False)
    """The Git the agent commits with; real Git until a world binds its own (``use``)."""

    def __post_init__(self) -> None:
        """Start on real Git, as a standalone writer would."""
        self.vcs = CliWorldGit(self.root, REAL_GIT)

    def use(self, vcs: WorldGit) -> None:
        """Commit through ``vcs``, the Git of the world this writer plays in."""
        self.vcs = vcs

    def worktree(self) -> Path:
        """The path of the run's single candidate worktree."""
        paths = self.vcs.worktrees()
        # An attempt's candidate lives in a member directory ``m-attempt-<id>``; the
        # evaluation executor keeps its own measurement worktrees (``s<id>``) beside it.
        candidates = [path for path in paths if path.parent.name.startswith("m-attempt-")]
        # A probe that runs a turn with no attempt has only the root checkout to write in.
        candidates = candidates or [self.root]
        assert len(candidates) == 1, paths
        return candidates[0]

    def __call__(self, request: AgentTurnRequest) -> None:
        """Write and commit one change, then name the commit in the reply."""
        self.invocations.append(request.invocation_id)
        self.commit_change()

    def commit_change(self) -> None:
        """Write and commit one change; ``answer`` then names the commit."""
        self.turns += 1
        tree = self.worktree()
        (tree / "candidate.py").write_text(f"VALUE = {self.turns + 1}\n", encoding="utf-8")
        self.answer["commit"] = self.vcs.commit_all(tree, f"implement {self.turns}")


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


def _open_agents(root: Path, threads: Threads) -> SessionHost:
    writer = CandidateWriter(root)
    client = AgentClient(
        FakeProvider(answer=writer.answer, on_turn=writer, threads=threads),
        clock=ManualClock(),
        threads=threads,
    )
    resolver = CandidateResolver(
        root,
        TemplateRenderer(root),
        roles=frozenset({IMPLEMENTER}),
        schemas={IMPLEMENTATION: Implementation},
        writer=writer,
    )
    return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())


def _skeleton_operations(env: WorkspaceEnv, templates: Path) -> OperationCatalog:
    """The operations the skeleton search declares, with the real renderer as their owner."""
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "skeleton.j2").write_text("Make the candidate faster.\n", encoding="utf-8")
    (templates / "skeleton-resume.j2").write_text("Your evaluations are in.\n", encoding="utf-8")
    store = ArtifactStore(env.project.state.local_namespace(RUN_ID, "artifacts"))
    roles = operation_roles(OPERATIONS)
    return build_operation_catalog(
        roles,
        {
            OperationRole.RENDER_ARTIFACTS: RenderArtifactsOwner(
                TemplateRenderer(templates), store
            ),
            OperationRole.VERIFY_REVISION: _Unused(),
            OperationRole.INTERPRET_EVIDENCE: _Unused(),
        },
    )


class _Unused:
    """An operation the skeleton declares (core requires it) but never asks for."""

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        del context
        message = f"the skeleton asked for {request.kind}"
        raise AssertionError(message)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        del request, context
        return NotApplied()


@contextmanager
def open_skeleton_world(  # noqa: PLR0913  # lint-waiver: LW-731842 [PLR0913]; every parameter after tmp_path is an independent optional seam of the world, and a settings object would be a wrapper type each of ~40 call sites must build.
    tmp_path: Path,
    strategy: SkeletonStrategy | None = None,
    cluster: ScenarioCluster | None = None,
    timed: tuple[Clock, float] | None = None,
    agents: Callable[[Path], SessionHost] | None = None,
    git: GitKind = GitKind.FAKE,
    execution: ExecutionTopology | None = None,
) -> Iterator[World]:
    """A fresh run on in-memory Git (or ``git=GitKind.REAL``, real Git) with the Fake Slurm cluster.

    ``execution`` owns the threads and blocking boundary of every component in the world.
    Closes every host on exit.
    """
    with open_workspace_env(tmp_path, git=git, execution=execution) as env:
        vcs = world_git(tmp_path / "project", env.git_disk)
        hosted = (
            _open_agents(tmp_path / "project", env.execution.threads)
            if agents is None
            else agents(tmp_path / "project")
        )
        if isinstance(hosted.resolver, CandidateResolver) and hosted.resolver.writer is not None:
            hosted.resolver.writer.use(vcs)
        yield World(
            git=vcs,
            env=env,
            root=tmp_path / "project",
            cluster=cluster or ScenarioCluster(),
            agents=hosted,
            strategy=strategy or SkeletonStrategy(),
            operations=_skeleton_operations(env, tmp_path / "templates"),
            timed=timed,
        )
