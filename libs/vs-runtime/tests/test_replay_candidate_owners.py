"""A candidate crosses the real workspace, agent and evaluation owners on native replay."""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, cast

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from tests.support.session_world import SCHEMA, Reply

from vs_agent.api import (
    AgentBackend,
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentInvocationState,
    AgentSessionSpec,
    AgentSpec,
    AgentUsage,
    ClientAgentSessions,
)
from vs_agent.api.testing import FakeAgentClient
from vs_core.api import ArtifactId, ArtifactRef, RevisionRef
from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey, classify_context
from vs_project.api import FakeStateStore, adoption_branch
from vs_proto.api import (
    Ended,
    EpochStarted,
    Outcome,
    core_agent_facts,
    core_common,
    core_leaves,
    core_vocabulary,
    replay,
)
from vs_proto.api import replay_agent as agent
from vs_proto.api import replay_evaluation as evaluation
from vs_proto.api import replay_workspace as workspace
from vs_runtime.api import ArtifactStore, WorkspaceAccess, render_stage_failure
from vs_runtime.api.core import (
    ProductionSessionResolver,
    ResolverInputs,
    StoreWorkspaceReceipts,
)
from vs_runtime.api.core_values import to_proto
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    SemanticSlurmEvaluationExecutor,
    TrustedEvaluationPlan,
)
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    OwnerReplayEnvironment,
    ReplayAgentReply,
    ReplayCompleted,
    ReplayEnding,
    ReplayLaunch,
    ReplayLaunchResources,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayOwnerServices,
    ReplayParked,
    ReplayPlan,
    ReplayRunHost,
    ReplayRuntime,
    ReplaySessionServices,
    drive_replay,
    open_replay,
    replay_bindings,
    replay_measurement_plan,
    run_replay,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, plan
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_sim.api.testing import NO_PROCESSES, InlineBlockingRunner, swept
from vs_slurm.api import SlurmConfig, SlurmSshTransport

from .generated.vibesys.runtime.testing.v1.agent_replay_pb import AgentReplayResult
from .test_replay_evaluation import SCOPE
from .test_replay_evaluation import step as evaluation_step
from .test_replay_executor_routing import echo
from .test_replay_executor_routing import program as routing_program
from .test_replay_executor_routing import role as echo_role
from .test_replay_sessions import LateYields, encode
from .test_replay_workspaces import request as workspace_request
from .test_replay_workspaces import step as workspace_step
from .test_session_resolver import role
from .test_workspace_requests import _commits, _env, _git
from .test_writer_reconciliation import empty_barrier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
    from pathlib import Path

    from protobuf import Message

    from vs_agent.api.testing import FakeInvocation
    from vs_runtime.api import AgentRole
    from vs_runtime.api.replay import ReplayExecutor
    from vs_sim.api.testing import Sim

    from .test_workspace_requests import _Env


REPLIES = (ReplayAgentReply(SCHEMA, Reply, AgentReplayResult, encode),)


async def agent_step[T: Message](ctx: ReplayFlow, action: Oneof, reply: type[T]) -> T:
    return await ctx.call(
        ReplayCall(
            SemanticKey("agent." + action.field).item("round-1"),
            classify_context(
                agent.ReplayAgentRequest(
                    scope=SCOPE, admission="admit-1", within_seconds=30, action=action
                ),
                contract="agent.v1",
            ),
            "agent.v1",
            replay.ReplayPolicy(
                reply_type=reply.desc().type_name,
                reservation=replay.ReplayResources(
                    money_usd=1, input_tokens=100, output_tokens=100
                ),
            ),
            reply,
        )
    )


EFFECTS = (
    "evaluation.observe",
    "workspace.open",
    "agent.ensure",
    "agent.turn",
    "workspace.snapshot",
    "agent.close",
    "evaluation.submit",
    "evaluation.collect",
    "evaluation.close",
    "workspace.retain",
    "workspace.settle",
    "workspace.adopt",
)


def program(
    base: core_leaves.RevisionRef,
    prompt: core_common.ArtifactRef,
    facts: core_common.ArtifactRef,
    *,
    reply_version: str = "1",
) -> Callable[[ReplayFlow], Coroutine[object, object, object]]:
    async def root(ctx: ReplayFlow) -> object:
        async with ctx.iteration("candidate", 1):
            opened = await workspace_step(
                ctx,
                workspace_request(
                    Oneof(
                        "open",
                        workspace.ReplayOpenWorkspace(
                            mode=workspace.ReplayWorkspaceMode.ISOLATED, base=base
                        ),
                    )
                ),
            )
            assert opened.status is workspace.ReplayWorkspaceStatus.SUCCEEDED
            session = agent.ReplayAgentSession(
                id="worker",
                role="worker",
                access=agent.ReplayAgentAccess.WRITE_CANDIDATE,
                acquisition=agent.ReplaySessionAcquisition.FRESH,
                lifetime=agent.ReplaySessionLifetime.EPHEMERAL,
            )
            ensured = await agent_step(
                ctx,
                Oneof("ensure", agent.ReplayEnsureAgent(session=session)),
                agent.ReplayAgentResult,
            )
            assert ensured.session is not None
            session = deepcopy(session)
            session.id = ensured.session.root
            reply = await agent_step(
                ctx,
                Oneof(
                    "turn",
                    agent.ReplayAgentTurn(
                        session=session,
                        invocation="implement",
                        workspace=SCOPE,
                        prompts=[prompt],
                        artifact_dependencies=[facts],
                        output_schema=core_common.SchemaRef(name="reply", version=reply_version),
                        max_turns=1,
                    ),
                ),
                AgentReplayResult,
            )
            assert reply.facts is not None
            assert reply.facts.status is core_vocabulary.ObservationStatus.SUCCEEDED
            snapshot = await workspace_step(
                ctx,
                workspace_request(
                    Oneof(
                        "snapshot",
                        workspace.ReplaySnapshotWorkspace(
                            retention=workspace.ReplayRetention.WIP,
                            invocation=reply.facts.invocation,
                        ),
                    )
                ),
            )
            assert snapshot.revision is not None
            assert snapshot.revision.revision_id is not None
            closed = await agent_step(
                ctx,
                Oneof(
                    "close",
                    agent.ReplayCloseAgent(session=session.id, resource=ensured.resource),
                ),
                agent.ReplayAgentResult,
            )
            assert closed.released
            measured = replay_measurement_plan(
                plan(stages=("accuracy",), candidate=snapshot.revision.revision_id.root)
            )
            submission = await evaluation_step(
                ctx, Oneof("submit", evaluation.ReplaySubmitMeasurement(plan=measured))
            )
            assert submission.resource
            observed = submission
            deadline = ctx.clock() + 100
            while observed.job is None or not observed.job.terminal:
                assert ctx.clock() < deadline
                await ctx.timer(ctx.clock() + 1)
                sample = (
                    "initial" if observed.job is None else str(observed.job.observation_sequence)
                )
                observed = await evaluation_step(
                    ctx,
                    Oneof(
                        "observe",
                        evaluation.ReplayMeasurementJob(resource=submission.resource),
                    ),
                    sample=sample,
                )
            assert observed.job.status is core_vocabulary.ObservationStatus.SUCCEEDED
            collected = await evaluation_step(
                ctx,
                Oneof("collect", evaluation.ReplayMeasurementJob(resource=submission.resource)),
            )
            assert collected.job is not None
            assert collected.job.evidence
            closed_jobs = await evaluation_step(
                ctx, Oneof("close", evaluation.ReplayCloseMeasurements())
            )
            assert closed_jobs.released
            retained = await workspace_step(
                ctx,
                workspace_request(
                    Oneof(
                        "retain",
                        workspace.ReplayRetainWorkspace(
                            revision=snapshot.revision,
                            retention=workspace.ReplayRetention.CANDIDATE,
                        ),
                    )
                ),
            )
            assert retained.status is workspace.ReplayWorkspaceStatus.SUCCEEDED
            settled = await workspace_step(
                ctx, workspace_request(Oneof("settle", workspace.ReplaySettleWorkspace()))
            )
            assert settled.released
            evidence = deepcopy(collected.job.evidence)
            for item in evidence:
                item.acceptance_receipt = core_agent_facts.EvidenceAcceptanceReceipt(
                    source_request=item.source_request,
                    scope=item.scope,
                    observation_sequence=item.observation_sequence,
                    status=item.status,
                )
            return await workspace_step(
                ctx,
                workspace.ReplayWorkspaceRequest(
                    action=Oneof(
                        "adopt",
                        workspace.ReplayAdoptWorkspace(
                            revision=snapshot.revision,
                            settlement_id="candidate-1",
                            evidence=evidence,
                        ),
                    )
                ),
            )

    return root


class CandidateWorld:
    def __init__(self, env: _Env, sim: Sim) -> None:
        self.env, self.sim = env, sim
        initial = env.hosts[0]
        revision = initial.root.revision
        assert revision is not None
        base = to_proto(RevisionRef.of_git_commit(revision))
        assert isinstance(base, core_leaves.RevisionRef)
        threads = sim.threads()
        receipts = env.store()
        artifacts = ArtifactStore(env.project.state.local_namespace(env.run_id, "replay-artifacts"))

        def artifact(data: bytes) -> core_common.ArtifactRef:
            stored = artifacts.write(data)
            return cast(
                "core_common.ArtifactRef",
                to_proto(
                    ArtifactRef(
                        artifact_id=ArtifactId(root="artifact-" + stored.sha256[:12]),
                        digest=stored.sha256,
                    )
                ),
            )

        prompt, facts = (
            artifact(b"Work."),
            artifact(core_common.Identity(root="round-1").to_binary()),
        )

        def implement(call: FakeInvocation) -> None:
            (call.workspace / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")

        client = (
            FakeAgentClient(
                capabilities=AgentCapabilities(session_reuse=True, provider_session_resume=True),
                threads=threads,
            )
            .set_response("worker", Reply(value=7))
            .set_usage(AgentUsage(input_tokens=10, output_tokens=2, total_cost_usd=0.1))
            .on_invoke(implement)
        )
        cluster = ScenarioCluster()
        store = FakeStateStore()
        self.jobs: list[SemanticSlurmEvaluationExecutor] = []

        self.initial = initial
        self.receipts = receipts
        self.artifacts = artifacts
        self.threads = threads
        self.client = client
        self.cluster = cluster
        self.store = store
        self.barrier = empty_barrier(env.project)
        self.root = program(base, prompt, facts)
        self.drifted_root = program(
            base, artifact(b"Work using revised policy."), facts, reply_version="2"
        )

    def services(self, *, restart: bool = False) -> ReplayOwnerServices:
        resources = self.env.start_host() if restart else self.initial

        def spec(selected: AgentRole, path: Path) -> AgentSessionSpec:
            return AgentSessionSpec(
                role=selected.id,
                provider="fake",
                workspace=path,
                policy=AgentExecutionPolicy(require_enforcement=False),
            )

        resolver = ProductionSessionResolver(
            ResolverInputs(
                roles=(role(WorkspaceAccess.READ_WRITE),),
                schemas={SCHEMA: Reply},
                workspaces=resources,
                workspace_receipts=StoreWorkspaceReceipts(self.receipts),
                artifacts=self.artifacts,
                configuration=lambda selected: AgentExecutionConfiguration(
                    selected.id, AgentSpec(backend=AgentBackend.STUB, cli_timeout=30)
                ),
                session_spec=spec,
            )
        )
        sessions = ClientAgentSessions(
            self.client,
            self.env.project.state.local_namespace(self.env.run_id, "replay-agent").slot(
                "journal.json", AgentInvocationState
            ),
            threads=self.threads,
        )
        config = SlurmConfig(
            name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="host-a")
        )
        measured = SemanticSlurmEvaluationExecutor(
            config,
            SlurmExecutionPolicy(),
            SlurmEvaluationPlan(
                config_path=self.env.project.root / "slurm.toml",
                accuracy_command=("python", "accuracy.py"),
                benchmark_command=None,
            ),
            TrustedEvaluationPlan(accuracy_command="unused", benchmark_command=None),
            resources,
            self.env.project.state.local_namespace(self.env.run_id, "replay-evaluation"),
            self.env.project.root / "evaluation-handles",
            stage_failure_text=render_stage_failure,
            clock=self.sim.clock,
            sleeper=self.sim.clock,
            blocking=self.env.blocking,
            processes=NO_PROCESSES,
            cluster=self.cluster,
        )
        self.jobs.append(measured)
        return ReplayOwnerServices(
            receipts=self.receipts,
            writer_registry=self.barrier.registry,
            workspaces=resources,
            sessions=ReplaySessionServices(
                sessions, resolver, InlineBlockingRunner(), LateYields()
            ),
            evaluation=measured,
            workspace_within_seconds=100,
            evaluation_within_seconds=100,
        )

    def host(
        self,
        *,
        restart: bool = False,
        iterations: int = 1,
        allow_adoption: bool = True,
        operations: Mapping[type[Message], ReplayExecutor] | None = None,
    ) -> ReplayRunHost:
        executor = replay_bindings(
            self.services(restart=restart),
            ReplayOperationHost(self.env.run_id, self.store, self.sim.clock),
            REPLIES,
            allow_adoption=allow_adoption,
            operations=operations,
        )
        return ReplayRunHost(
            ReplayRuntime(self.store, self.root, replay.ReplayAllowance(iterations=iterations)),
            executor,
            self.sim.clock,
            lambda _: ReplayEnding(
                Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
            ),
            writer_barrier=self.barrier,
        )

    async def exercise(self, index: int, boundary: str) -> None:
        try:
            first = self.host()
            first.runtime.start(
                ReplayLaunch(EpochStarted(run_id=self.env.run_id), "first", "host-a", 100),
                now=self.sim.clock.now(),
                at=Timestamp(),
            )
            await self.barrier.reconcile(first.runtime, clock=self.sim.clock)
            while True:
                ready = first.runtime.prepare(now=self.sim.clock.now(), at=Timestamp())
                if not ready:
                    deadline = min(first.runtime.clock_requests())
                    await self.sim.clock.sleep(max(0, deadline - first.runtime.clock))
                    first.runtime.advance_clock(deadline, now=self.sim.clock.now(), at=Timestamp())
                    continue
                (span,) = ready
                selected = span.name == EFFECTS[index]
                if selected and boundary == "prepared":
                    break
                result = await first.runtime.dispatch(
                    span.id, first.executor, now=self.sim.clock.now()
                )
                if selected and boundary == "executed":
                    break
                first.runtime.observe(span.id, result, now=self.sim.clock.now(), at=Timestamp())
                if selected and boundary == "observed":
                    break
                first.runtime.accept(span.id, now=self.sim.clock.now(), at=Timestamp())
                if selected:
                    break
            first.runtime.close(now=self.sim.clock.now())
            resumed = self.host(restart=True, iterations=0)
            completed = await run_replay(
                resumed, ReplayLaunch(EpochStarted(run_id=self.env.run_id), "resume", "host-a", 100)
            )
            assert isinstance(completed, ReplayCompleted)
            assert isinstance(completed.value, workspace.ReplayWorkspaceResult)
            assert completed.value.status is workspace.ReplayWorkspaceStatus.SUCCEEDED
            # Untracked evaluation handles keep the checkout dirty, so publication
            # records the candidate and the root file stays as the run left it.
            assert (self.initial.root.path / "candidate.py").read_text() == "VALUE = 1\n"
            assert (
                _git(
                    self.initial.root.path,
                    "show",
                    f"{adoption_branch(self.env.run_id)}:candidate.py",
                )
                == "VALUE = 2"
            )
            assert len(self.client.calls_for("worker")) == 1
            assert len(self.cluster.submissions) == 1
            assert resumed.runtime.accounting.lifetime_completed == 1
            assert resumed.runtime.accounting.reported_cost_usd == pytest.approx(0.1)
            commits = _commits(self.initial)
            self.root = self.drifted_root
            replayed = self.host(restart=True, iterations=0)
            assert (
                await run_replay(
                    replayed,
                    ReplayLaunch(EpochStarted(run_id=self.env.run_id), "again", "host-a", 100),
                )
                == completed
            )
            assert _commits(self.initial) == commits
            assert len(self.client.calls_for("worker")) == 1
            assert len(self.cluster.submissions) == 1
            assert replayed.runtime.accounting.lifetime_completed == 1
            assert replayed.runtime.accounting.reported_cost_usd == pytest.approx(0.1)
        finally:
            for executor in self.jobs:
                await executor.close()


@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
def test_composed_candidate_recovers_with_real_content_and_evidence(
    tmp_path_factory: pytest.TempPathFactory, sim: Sim, boundary: str
) -> None:
    # PR depth covers every boundary of a job accepted by the old host. Nightly
    # depth also sweeps all other effects without multiplying the seed job cost.
    for index in swept(len(EFFECTS), sample=1):
        with _env(tmp_path_factory.mktemp(f"candidate-{index}")) as env:
            world = CandidateWorld(env, sim)
            sim.run(world.exercise(index, boundary), threads=world.threads)


def test_zero_fresh_iteration_budget_never_reaches_an_effect_owner(
    tmp_path: Path, sim: Sim
) -> None:
    with _env(tmp_path) as env:
        world = CandidateWorld(env, sim)

        async def scenario() -> None:
            try:
                host = world.host(iterations=0)
                result = await run_replay(
                    host, ReplayLaunch(EpochStarted(run_id=env.run_id), "first", "host-a", 100)
                )
                assert isinstance(result, ReplayParked)
                assert not world.client.calls_for("worker")
                assert not world.cluster.submissions
                assert host.runtime.accounting.lifetime_completed == 0
                assert host.runtime.accounting.paid_operations == 0
                assert not [
                    span
                    for span in host.runtime.archive.spans
                    if span.replay is not None
                    and span.replay.key is not None
                    and not span.replay.key.operation.startswith("framework.")
                ]
            finally:
                for job in world.jobs:
                    await job.close()

        sim.run(scenario(), threads=world.threads)


def test_native_composition_preserves_operator_edits_when_adoption_is_protected(
    tmp_path: Path, sim: Sim
) -> None:
    with _env(tmp_path) as env:
        world = CandidateWorld(env, sim)
        root = world.initial.root.path
        (root / "candidate.py").write_text("VALUE = 9\n", encoding="utf-8")
        (root / "operator-note.txt").write_text("Keep this.\n", encoding="utf-8")
        original_head = world.initial.root.revision

        async def scenario() -> None:
            try:
                result = await run_replay(
                    world.host(allow_adoption=False),
                    ReplayLaunch(EpochStarted(run_id=env.run_id), "first", "host-a", 100),
                )
                assert isinstance(result, ReplayCompleted)
                assert isinstance(result.value, workspace.ReplayWorkspaceResult)
                assert result.value.status is workspace.ReplayWorkspaceStatus.REJECTED
                assert (root / "candidate.py").read_text() == "VALUE = 9\n"
                assert (root / "operator-note.txt").read_text() == "Keep this.\n"
                assert world.initial.root.revision == original_head
                assert len(world.client.calls_for("worker")) == 1
                assert len(world.cluster.submissions) == 1
            finally:
                for job in world.jobs:
                    await job.close()

        sim.run(scenario(), threads=world.threads)


def test_a_resume_launch_never_restores_the_operator_root(tmp_path: Path, sim: Sim) -> None:
    with _env(tmp_path) as env:
        world = CandidateWorld(env, sim)
        root = world.initial.root.path
        original_head = world.initial.root.revision
        plan = ReplayPlan(
            world.root,
            replay.ReplayAllowance(iterations=1),
            lambda _: ReplayEnding(
                Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
            ),
            REPLIES,
            (),
            (),
        )
        opened: list[bool] = []

        @asynccontextmanager
        async def environment() -> AsyncIterator[OwnerReplayEnvironment]:
            opened.append(True)
            if len(opened) == 1:
                problem = "the run environment did not open"
                raise OSError(problem)
            yield OwnerReplayEnvironment(world.services(), world.barrier, None)

        resources = ReplayLaunchResources(world.store, sim.clock, environment, None, 1)

        async def scenario() -> None:
            try:
                first = ReplayLaunch(EpochStarted(run_id=env.run_id), "first", "host-a", 100)
                with pytest.raises(OSError, match="did not open"):
                    async with open_replay(plan, first, resources):
                        pytest.fail("a failed setup opened a lifetime")
                # The operator edits the root while the run is interrupted, then resumes.
                (root / "candidate.py").write_text("VALUE = 9\n", encoding="utf-8")
                (root / "operator-note.txt").write_text("Keep this.\n", encoding="utf-8")
                resume = ReplayLaunch(EpochStarted(run_id=env.run_id), "resume", "host-a", 100)
                async with open_replay(plan, resume, resources) as lifetime:
                    result = await drive_replay(lifetime)
                assert isinstance(result, ReplayCompleted)
                assert isinstance(result.value, workspace.ReplayWorkspaceResult)
                assert result.value.status is workspace.ReplayWorkspaceStatus.REJECTED
                assert (root / "candidate.py").read_text() == "VALUE = 9\n"
                assert (root / "operator-note.txt").read_text() == "Keep this.\n"
                assert world.initial.root.revision == original_head
            finally:
                for job in world.jobs:
                    await job.close()

        sim.run(scenario(), threads=world.threads)


def test_native_composition_adds_registered_request_owners(tmp_path: Path, sim: Sim) -> None:
    with _env(tmp_path) as env:
        world = CandidateWorld(env, sim)
        world.root = routing_program
        first, second = FakeReplayExecutor(echo), FakeReplayExecutor(echo_role)

        async def scenario() -> None:
            try:
                completed = await run_replay(
                    world.host(
                        operations={core_common.Identity: first, core_common.RoleId: second}
                    ),
                    ReplayLaunch(EpochStarted(run_id=env.run_id), "first", "host-a", 100),
                )
                assert completed == ReplayCompleted(("a", "role:b"))
                assert len(first.invocations) == len(second.invocations) == 1
                assert not world.client.calls_for("worker")
                assert not world.cluster.submissions
            finally:
                for job in world.jobs:
                    await job.close()

        sim.run(scenario(), threads=world.threads)


@pytest.mark.parametrize(
    "request_type",
    [
        workspace.ReplayWorkspaceRequest,
        agent.ReplayAgentRequest,
        evaluation.ReplayEvaluationRequest,
    ],
)
def test_registered_request_owners_cannot_replace_native_lifecycle_owners(
    tmp_path: Path, sim: Sim, request_type: type[Message]
) -> None:
    with _env(tmp_path) as env:
        world = CandidateWorld(env, sim)

        async def scenario() -> None:
            try:
                with pytest.raises(ReplayOperationError, match="cannot replace lifecycle owner"):
                    world.host(operations={request_type: FakeReplayExecutor(echo)})
                assert not world.client.calls_for("worker")
                assert not world.cluster.submissions
            finally:
                for job in world.jobs:
                    await job.close()

        sim.run(scenario(), threads=world.threads)
