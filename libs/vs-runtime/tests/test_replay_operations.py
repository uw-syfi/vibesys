"""Keyed dispatch uses the real owner's execution and recovery path."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from protobuf import Message
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from pydantic import BaseModel

from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey
from vs_project.api import FakeStateStore, Project
from vs_proto.api import core_common, replay
from vs_runtime.api.core import (
    ReceiptStore,
)
from vs_runtime.api.operation_fixtures import (
    EchoOutcome,
    EchoRequest,
    catalog_of,
    scenarios,
)
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    RegisteredReplayExecutor,
    ReplayAbsent,
    ReplayCompleted,
    ReplayExecutor,
    ReplayOperationBinding,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayResult,
    ReplayUnknown,
    drive_replay,
)
from vs_sim.api.testing import Sim, SimThreads

from .test_replay_run import host, launch

Reply = core_common.Identity


def decode(value: Message) -> EchoRequest:
    assert isinstance(value, Reply)
    return EchoRequest(text=value.root)


def encode(value: BaseModel) -> Reply:
    assert isinstance(value, EchoOutcome)
    return Reply(root=value.text)


BINDING = ReplayOperationBinding("echo", "test.echo", Reply, Reply, decode, encode, 60, paid=False)


async def root(ctx: ReplayFlow) -> str:
    reply = await ctx.call(
        ReplayCall(
            SemanticKey("echo"),
            Reply(root="answer"),
            "echo.v1",
            replay.ReplayPolicy(reply_type=Reply.desc().type_name),
            Reply,
        )
    )
    return reply.root


@dataclass(frozen=True)
class World:
    store: FakeStateStore
    executor: Callable[[], ReplayExecutor]
    effects: Callable[[], int]


def world(tmp_path: Path, sim: Sim, implementation: str) -> World:
    store = FakeStateStore()
    if implementation == "fake":
        executed: list[str] = []

        def perform(span: object) -> ReplayResult:
            del span
            executed.append("answer")
            return ReplayResult(Reply(root="answer"))

        fake = FakeReplayExecutor(perform)
        return World(store, lambda: fake, lambda: len(executed))
    project = Project.open(tmp_path)
    namespace = project.state.state_store_namespace("run")
    cases = scenarios(tmp_path / "owners", namespace)
    echo = next(case for case in cases if case.name == "echo")
    catalog = catalog_of((echo,))

    def executor() -> RegisteredReplayExecutor:
        receipts = ReceiptStore(namespace, SimThreads())
        return RegisteredReplayExecutor(
            catalog,
            receipts,
            (BINDING,),
            host=ReplayOperationHost("run", store, sim.clock),
        )

    return World(store, executor, echo.effects)


@pytest.mark.parametrize("implementation", ["fake", "registered"])
@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
async def test_owner_recovers_at_every_replay_effect_boundary(
    tmp_path: Path, sim: Sim, boundary: str, implementation: str
) -> None:
    given = world(tmp_path, sim, implementation)
    store, executor = given.store, given.executor
    first_executor = executor()
    first = host(store, root, first_executor, sim).runtime
    first.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = first.prepare(now=sim.clock.now(), at=Timestamp())
    if boundary != "prepared":
        result = await first.dispatch(span.id, first_executor, now=sim.clock.now())
        assert result == ReplayResult(Reply(root="answer"))
        if boundary != "executed":
            first.observe(span.id, result, now=sim.clock.now(), at=Timestamp())
            if boundary == "accepted":
                first.accept(span.id, now=sim.clock.now(), at=Timestamp())
    first.close(now=sim.clock.now())
    assert await drive_replay(host(store, root, executor(), sim), launch("resume")) == (
        ReplayCompleted("answer")
    )
    assert given.effects() == 1
    assert await drive_replay(host(store, root, executor(), sim), launch("again")) == (
        ReplayCompleted("answer")
    )
    assert given.effects() == 1


async def test_real_registered_owner_refuses_stale_authority_before_effect(
    tmp_path: Path, sim: Sim
) -> None:
    project = Project.open(tmp_path)
    namespace = project.state.state_store_namespace("run")
    echo = next(case for case in scenarios(tmp_path / "owners", namespace) if case.name == "echo")
    catalog = catalog_of((echo,))
    receipts = ReceiptStore(namespace, SimThreads())
    store = FakeStateStore()
    executor = RegisteredReplayExecutor(
        catalog,
        receipts,
        (BINDING,),
        host=ReplayOperationHost("run", store, sim.clock),
    )
    runtime = host(store, root, executor, sim).runtime
    runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
    runtime.close(now=sim.clock.now())
    fence = store.acquire("old", sim.clock.now(), 1)
    assert fence is not None
    assert store.release(fence, sim.clock.now())
    with pytest.raises(ReplayOperationError, match="no conclusive typed outcome"):
        await executor.execute(span, fence)
    assert echo.effects() == 0


@pytest.mark.parametrize("implementation", ["fake", "registered"])
async def test_same_physical_invocation_replays_and_rejects_another_payload(
    tmp_path: Path, sim: Sim, implementation: str
) -> None:
    given = world(tmp_path, sim, implementation)
    executor = given.executor()
    runtime = host(given.store, root, executor, sim).runtime
    runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
    runtime.close(now=sim.clock.now())
    fence = given.store.acquire("host-a", sim.clock.now(), 10)
    assert fence is not None
    assert await executor.execute(span, fence) == ReplayResult(Reply(root="answer"))
    assert await given.executor().execute(span, fence) == ReplayResult(Reply(root="answer"))
    assert given.effects() == 1
    assert span.replay is not None
    assert span.replay.policy is not None
    assert span.replay.policy.request is not None
    span.replay.policy.request.value = Reply(root="changed").to_binary()
    with pytest.raises(ValueError, match=r"conflict|no conclusive typed outcome"):
        await given.executor().execute(span, fence)
    with pytest.raises(ValueError, match="conflict"):
        await given.executor().inspect(span, fence)
    assert given.effects() == 1
    assert given.store.release(fence, sim.clock.now())


@pytest.mark.regression
@pytest.mark.parametrize("implementation", ["fake", "registered"])
async def test_confirmed_absence_withdraws_the_old_physical_invocation(
    tmp_path: Path, sim: Sim, implementation: str
) -> None:
    given = world(tmp_path, sim, implementation)
    executor = given.executor()
    runtime = host(given.store, root, executor, sim).runtime
    runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
    runtime.close(now=sim.clock.now())
    fence = given.store.acquire("host-a", sim.clock.now(), 10)
    assert fence is not None
    assert await executor.inspect(span, fence) == ReplayAbsent()
    with pytest.raises(ValueError, match=r"withdrawn|no conclusive typed outcome"):
        await given.executor().execute(span, fence)
    assert await executor.inspect(span, fence) == ReplayAbsent()
    assert given.effects() == 0
    assert given.store.release(fence, sim.clock.now())


@pytest.mark.parametrize(
    ("mutation", "problem"),
    [
        ("operation", "unbound replay operation"),
        ("missing", "request type differs"),
        ("request_type", "request type differs"),
        ("reply_type", "reply type differs"),
        ("unknown_field", "unknown fields"),
    ],
)
async def test_native_request_contract_is_checked_before_the_owner_runs(
    tmp_path: Path, sim: Sim, mutation: str, problem: str
) -> None:
    given = world(tmp_path, sim, "registered")
    executor = given.executor()
    runtime = host(given.store, root, executor, sim).runtime
    runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
    runtime.close(now=sim.clock.now())
    assert span.replay is not None
    assert span.replay.key is not None
    assert span.replay.policy is not None
    policy = span.replay.policy
    assert policy.request is not None
    match mutation:
        case "operation":
            span.replay.key.operation = "unbound"
        case "missing":
            policy.request = None
        case "request_type":
            policy.request.type_url = "unknown.Request"
        case "reply_type":
            policy.reply_type = "unknown.Reply"
        case "unknown_field":
            policy.request.value += b"\x10\x01"
    fence = given.store.acquire("host-a", sim.clock.now(), 10)
    assert fence is not None
    with pytest.raises(ReplayOperationError, match=problem):
        await executor.execute(span, fence)
    with pytest.raises(ReplayOperationError, match=problem):
        await executor.inspect(span, fence)
    assert given.effects() == 0
    assert given.store.release(fence, sim.clock.now())


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_native_bindings_reject_unbounded_execution(duration: float) -> None:
    with pytest.raises(ReplayOperationError, match="finite and positive"):
        replace(BINDING, within_seconds=duration)


async def test_lost_lease_inspection_never_claims_absence(tmp_path: Path, sim: Sim) -> None:
    given = world(tmp_path, sim, "registered")
    executor = given.executor()
    runtime = host(given.store, root, executor, sim).runtime
    runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
    runtime.close(now=sim.clock.now())
    fence = given.store.acquire("host-a", sim.clock.now(), 10)
    assert fence is not None
    assert given.store.release(fence, sim.clock.now())
    assert isinstance(await executor.inspect(span, fence), ReplayUnknown)
    assert given.effects() == 0
