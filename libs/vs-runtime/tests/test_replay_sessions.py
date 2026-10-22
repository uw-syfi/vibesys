"""Native agent replay against Fake and durable production session journals."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from tests.support.session_world import SCHEMA, FakeSessionResolver, Reply

from vs_agent.api import AgentCapabilities, AgentInvocationState, AgentUsage, ClientAgentSessions
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore, FakeAgentSessions
from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey, classify_context
from vs_project.api import FakeStateStore, Project
from vs_prompts.api import TemplateRenderer
from vs_proto.api import EpochStarted, core_common, core_vocabulary, replay
from vs_proto.api import replay_agent as pb
from vs_runtime.api.core import ReceiptStore
from vs_runtime.api.executor_contracts import FaultingNamespace, ProcessKilledError
from vs_runtime.api.replay import (
    ReplayAgentReply,
    ReplayCompleted,
    ReplayLaunch,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayRecoveryRequiredError,
    ReplayRuntime,
    ReplaySessionServices,
    ReplayUnknown,
    SessionReplayExecutor,
    drive_replay,
)
from vs_sim.api.testing import SimBlockingRunner, swept

from .generated.vibesys.runtime.testing.v1.agent_replay_pb import AgentReplayResult
from .test_replay_run import host

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from protobuf import Message
    from pydantic import BaseModel

    from vs_project.api import StateNamespace
    from vs_sim.api.testing import Sim, SimThreads


SCOPE = core_common.Scope(
    owner=core_common.ScopeOwner(owner=Oneof("run", core_common.RunId(root="run", kind="run"))),
    generation="0",
)
SESSION = pb.ReplayAgentSession(
    id="session-1",
    role="worker",
    access=pb.ReplayAgentAccess.READ_ONLY,
    acquisition=pb.ReplaySessionAcquisition.FRESH,
    lifetime=pb.ReplaySessionLifetime.OWNER,
)


def encode(facts: pb.ReplayAgentResult, value: BaseModel | None) -> Message:
    assert value is None or isinstance(value, Reply)
    return AgentReplayResult(facts=facts, value=None if value is None else value.value)


async def step[T: Message](ctx: ReplayFlow, action: Oneof, reply: type[T]) -> T:
    name = action.field
    suffix = action.value.invocation if name == "turn" else name
    return await ctx.call(
        ReplayCall(
            SemanticKey("agent." + name).item(suffix),
            classify_context(
                pb.ReplayAgentRequest(scope=SCOPE, within_seconds=30, action=action),
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


async def program(ctx: ReplayFlow) -> object:
    ensured = await step(
        ctx, Oneof("ensure", pb.ReplayEnsureAgent(session=SESSION)), pb.ReplayAgentResult
    )
    assert ensured.status is core_vocabulary.ObservationStatus.SUCCEEDED
    assert ensured.session is not None
    session = deepcopy(SESSION)
    session.id = ensured.session.root
    for invocation in ("first", "second"):
        result = await step(
            ctx,
            Oneof(
                "turn",
                pb.ReplayAgentTurn(
                    session=session,
                    invocation=invocation,
                    workspace=SCOPE,
                    prompts=[
                        core_common.ArtifactRef(
                            artifact_id=core_common.ArtifactId(root="prompt", kind="artifact"),
                            digest="sha256:0",
                        )
                    ],
                    artifact_dependencies=[
                        core_common.ArtifactRef(
                            artifact_id=core_common.ArtifactId(root="context", kind="artifact"),
                            digest="sha256:context",
                        )
                    ],
                    output_schema=core_common.SchemaRef(name="reply", version="1"),
                    max_turns=1,
                ),
            ),
            AgentReplayResult,
        )
        assert result.facts is not None
        assert result.facts.status is core_vocabulary.ObservationStatus.SUCCEEDED
        assert result.value == 7
    assert result.facts.invocation is not None
    cancelled = await step(
        ctx,
        Oneof("cancel", pb.ReplayCancelAgent(invocation=result.facts.invocation)),
        pb.ReplayAgentResult,
    )
    assert cancelled.released
    closed = await step(
        ctx,
        Oneof(
            "close", pb.ReplayCloseAgent(session=ensured.session.root, resource=ensured.resource)
        ),
        pb.ReplayAgentResult,
    )
    assert closed.released
    return 7


@dataclass(frozen=True)
class AgentWorld:
    store: FakeStateStore
    client: FakeAgentClient
    threads: SimThreads
    namespace: StateNamespace
    resolver: FakeSessionResolver
    executor: Callable[[ReceiptStore | None], SessionReplayExecutor]


def world(tmp_path: Path, sim: Sim, implementation: str) -> AgentWorld:
    project = Project.open(tmp_path)
    namespace = project.state.state_store_namespace("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    threads = sim.threads()
    client = (
        FakeAgentClient(
            capabilities=AgentCapabilities(session_reuse=True, provider_session_resume=True),
            threads=threads,
        )
        .set_response("worker", Reply(value=7))
        .set_usage(AgentUsage(input_tokens=10, output_tokens=2, total_cost_usd=0.1))
    )
    journal = (
        namespace.slot("journal.json", AgentInvocationState)
        if implementation == "client"
        else FakeAgentInvocationStore()
    )
    store = FakeStateStore()
    resolver = FakeSessionResolver(workspace, TemplateRenderer(workspace))

    def executor(receipts: ReceiptStore | None = None) -> SessionReplayExecutor:
        factory = ClientAgentSessions if implementation == "client" else FakeAgentSessions
        return SessionReplayExecutor(
            ReplaySessionServices(
                factory(client, journal, threads=threads), resolver, SimBlockingRunner(threads)
            ),
            ReceiptStore(namespace, threads) if receipts is None else receipts,
            ReplayOperationHost("run", store, sim.clock),
            (ReplayAgentReply(SCHEMA, Reply, AgentReplayResult, encode),),
        )

    return AgentWorld(store, client, threads, namespace, resolver, executor)


@pytest.mark.parametrize("implementation", ["fake", "client"])
@pytest.mark.parametrize("effect", range(5))
@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
def test_agent_owner_recovery_never_repeats_provider_work(
    tmp_path: Path, sim: Sim, implementation: str, effect: int, boundary: str
) -> None:
    given = world(tmp_path, sim, implementation)
    store, client, threads = given.store, given.client, given.threads

    def executor() -> SessionReplayExecutor:
        return given.executor(None)

    async def scenario() -> None:
        first_executor = executor()
        runtime = ReplayRuntime(store, program, replay.ReplayAllowance(iterations=1))
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        for ordinal in range(effect + 1):
            (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
            if ordinal == effect and boundary == "prepared":
                break
            result = await runtime.dispatch(span.id, first_executor, now=sim.clock.now())
            if ordinal == effect and boundary == "executed":
                break
            runtime.observe(span.id, result, now=sim.clock.now(), at=Timestamp())
            if ordinal == effect and boundary == "observed":
                break
            runtime.accept(span.id, now=sim.clock.now(), at=Timestamp())
        runtime.close(now=sim.clock.now())
        launch = ReplayLaunch(EpochStarted(run_id="run"), "resume", "host-a", 100)
        assert await drive_replay(host(store, program, executor(), sim), launch) == ReplayCompleted(
            7
        )
        assert len(client.calls_for("worker")) == 2
        launch = ReplayLaunch(EpochStarted(run_id="run"), "again", "host-a", 100)
        final = host(store, program, executor(), sim)
        assert await drive_replay(final, launch) == ReplayCompleted(7)
        assert len(client.calls_for("worker")) == 2
        assert final.runtime.accounting.paid_operations == 2
        assert final.runtime.accounting.reported_cost_usd == pytest.approx(0.2)
        assert final.runtime.accounting.unreported_cost_operations == 0

    sim.run(scenario(), threads=threads)


@pytest.mark.parametrize("implementation", ["fake", "client"])
def test_agent_recovery_at_owner_receipt_writes(
    tmp_path: Path, sim: Sim, implementation: str
) -> None:
    def run_case(root: Path, crash_at: int | None) -> int:
        root.mkdir()
        given = world(root, sim, implementation)
        faulting = FaultingNamespace(given.namespace, crash_at)
        receipts = ReceiptStore(cast("StateNamespace", faulting), given.threads)

        async def scenario() -> None:
            first = host(given.store, program, given.executor(receipts), sim)
            launch = ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100)
            if crash_at is None:
                assert await drive_replay(first, launch) == ReplayCompleted(7)
            else:
                with pytest.raises(ProcessKilledError):
                    await drive_replay(first, launch)
                resumed = host(given.store, program, given.executor(None), sim)
                launch = ReplayLaunch(EpochStarted(run_id="run"), "resume", "host-a", 100)
                assert await drive_replay(resumed, launch) == ReplayCompleted(7)
                assert resumed.runtime.accounting.reported_cost_usd == pytest.approx(0.2)
            assert len(given.client.calls_for("worker")) == 2

        sim.run(scenario(), threads=given.threads)
        return faulting.writes

    writes = run_case(tmp_path / "baseline", None)
    for boundary in swept(2 * writes, sample=3):
        run_case(tmp_path / f"crash-{boundary}", boundary)


@pytest.mark.parametrize("implementation", ["fake", "client"])
def test_agent_physical_attempts_are_distinct_and_payload_fenced(
    tmp_path: Path, sim: Sim, implementation: str
) -> None:
    given = world(tmp_path, sim, implementation)

    async def scenario() -> None:
        executor = given.executor(None)
        runtime = ReplayRuntime(given.store, program, replay.ReplayAllowance())
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        (ensure,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
        result = await runtime.dispatch(ensure.id, executor, now=sim.clock.now())
        runtime.observe(ensure.id, result, now=sim.clock.now(), at=Timestamp())
        runtime.accept(ensure.id, now=sim.clock.now(), at=Timestamp())
        (turn,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
        runtime.close(now=sim.clock.now())
        fence = given.store.acquire("host-a", sim.clock.now(), 100)
        assert fence is not None
        first = await executor.execute(turn, fence)
        assert await given.executor(None).execute(turn, fence) == first
        another = deepcopy(turn)
        another.id += "/another-attempt"
        second = await given.executor(None).execute(another, fence)
        assert isinstance(first.outcome, AgentReplayResult)
        assert isinstance(second.outcome, AgentReplayResult)
        assert first.outcome.facts is not None
        assert second.outcome.facts is not None
        assert first.outcome.facts.invocation != second.outcome.facts.invocation
        assert len(given.client.calls_for("worker")) == 2
        assert turn.replay is not None
        assert turn.replay.policy is not None
        assert turn.replay.policy.request is not None
        request = pb.ReplayAgentRequest.from_binary(turn.replay.policy.request.value)
        request.within_seconds = 31
        turn.replay.policy.request.value = request.to_binary()
        for method in (executor.execute, executor.inspect):
            with pytest.raises(ReplayOperationError, match="payload conflict"):
                await method(turn, fence)
        assert given.store.release(fence, sim.clock.now())
        assert isinstance(await executor.inspect(turn, fence), ReplayUnknown)
        with pytest.raises(ReplayOperationError, match="lease was lost"):
            await executor.execute(turn, fence)
        assert len(given.client.calls_for("worker")) == 2

    sim.run(scenario(), threads=given.threads)


@pytest.mark.parametrize("implementation", ["fake", "client"])
def test_unknown_provider_acceptance_is_never_redispatched(
    tmp_path: Path, sim: Sim, implementation: str
) -> None:
    given = world(tmp_path, sim, implementation)
    given.client.fail("worker", ConnectionError("provider lost acceptance"), times=1)

    async def scenario() -> None:
        first = host(given.store, program, given.executor(None), sim)
        launch = ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100)
        with pytest.raises(ReplayOperationError, match="remains unknown"):
            await drive_replay(first, launch)
        assert len(given.client.calls_for("worker")) == 1
        assert first.runtime.accounting.resources.committed.money_usd == 1
        resumed = host(given.store, program, given.executor(None), sim)
        launch = ReplayLaunch(EpochStarted(run_id="run"), "resume", "host-a", 100)
        with pytest.raises(ReplayRecoveryRequiredError):
            await drive_replay(resumed, launch)
        assert len(given.client.calls_for("worker")) == 1

    sim.run(scenario(), threads=given.threads)
