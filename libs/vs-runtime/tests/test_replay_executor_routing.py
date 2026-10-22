"""A run's closed native owner map cannot guess a route or alias a schema name."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey
from vs_project.api import FakeStateStore
from vs_proto.api import Record, core_common, replay
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCompleted,
    ReplayExecutors,
    ReplayOperationError,
    ReplayResult,
    run_replay,
)

from .test_replay_p1_record_compatibility import p1_contract
from .test_replay_run import host, launch

if TYPE_CHECKING:
    from vs_proto.api import Span
    from vs_sim.api.testing import Sim


def echo(span: Span) -> ReplayResult:
    assert span.replay is not None
    assert span.replay.policy is not None
    assert span.replay.policy.request is not None
    return ReplayResult(core_common.Identity.from_binary(span.replay.policy.request.value))


def role(span: Span) -> ReplayResult:
    assert span.replay is not None
    assert span.replay.policy is not None
    assert span.replay.policy.request is not None
    value = core_common.RoleId.from_binary(span.replay.policy.request.value)
    return ReplayResult(core_common.Identity(root="role:" + value.root))


async def program(ctx: ReplayFlow) -> object:
    first = await ctx.call(
        ReplayCall(
            SemanticKey("echo"),
            core_common.Identity(root="a"),
            "echo.v1",
            replay.ReplayPolicy(reply_type=core_common.Identity.desc().type_name),
            core_common.Identity,
        )
    )
    second = await ctx.call(
        ReplayCall(
            SemanticKey("role"),
            core_common.RoleId(root="b", kind="role"),
            "role.v1",
            replay.ReplayPolicy(reply_type=core_common.Identity.desc().type_name),
            core_common.Identity,
        )
    )
    return first.root, second.root


async def test_closed_owner_map_routes_execution_and_recovery_by_native_type(sim: Sim) -> None:
    first, second = FakeReplayExecutor(echo), FakeReplayExecutor(role)
    owners = ReplayExecutors({core_common.Identity: first, core_common.RoleId: second})
    store = FakeStateStore()
    runtime = host(store, program, owners, sim).runtime
    runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
    await runtime.dispatch(span.id, owners, now=sim.clock.now())
    runtime.close(now=sim.clock.now())
    assert await run_replay(host(store, program, owners, sim), launch("resume")) == ReplayCompleted(
        ("a", "role:b")
    )
    assert len(first.invocations) == 1
    assert len(second.invocations) == 1


@pytest.mark.parametrize("mutation", ["missing_metadata", "missing_request", "unknown_type"])
async def test_unknown_native_routing_keys_never_reach_an_owner(sim: Sim, mutation: str) -> None:
    first = FakeReplayExecutor(echo)
    owners = ReplayExecutors({core_common.Identity: first})
    store = FakeStateStore()
    runtime = host(store, program, owners, sim).runtime
    runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
    runtime.close(now=sim.clock.now())
    assert span.replay is not None
    assert span.replay.policy is not None
    assert span.replay.policy.request is not None
    match mutation:
        case "missing_metadata":
            span.replay = None
        case "missing_request":
            span.replay.policy.request = None
        case "unknown_type":
            span.replay.policy.request.type_url = "unknown.Request"
    fence = store.acquire("host-a", sim.clock.now(), 100)
    assert fence is not None
    for method in (owners.execute, owners.inspect):
        with pytest.raises(
            ReplayOperationError,
            match=r"requires an exact native request|unbound replay native request: unknown.Request",
        ):
            await method(span, fence)
    assert not first.invocations
    assert store.release(fence, sim.clock.now())


def test_two_generated_classes_cannot_bind_different_owners_to_one_type_name() -> None:
    historical = p1_contract().Record
    assert historical is not Record
    assert historical.desc().type_name == Record.desc().type_name
    with pytest.raises(ReplayOperationError, match="duplicate replay owner"):
        ReplayExecutors({historical: FakeReplayExecutor(echo), Record: FakeReplayExecutor(role)})
