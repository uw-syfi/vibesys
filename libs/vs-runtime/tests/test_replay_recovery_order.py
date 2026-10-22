"""Recovery closes uncertain writers before owner inspection can acquire their backing."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from protobuf.wkt.google.protobuf.wrappers_pb import StringValue

from vs_core.api.replay import ReplayCall, ReplayFlow, ReplayWriterRecovery, SemanticKey
from vs_project.api import FakeStateStore, Project, StoredEnvelope
from vs_proto.api import ReplayStore as Records
from vs_proto.api import core_common, replay
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayAbsent,
    ReplayCompleted,
    ReplayExecutors,
    ReplayPending,
    ReplayResult,
    ReplayUnknown,
    WriterRegistry,
    run_replay,
)

from .test_replay_physical import physical_root, proof_barrier
from .test_replay_run import host, launch

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StoreFence
    from vs_proto.api import Span
    from vs_sim.api.testing import Sim


async def program(ctx: ReplayFlow) -> object:
    async def read(ctx: ReplayFlow) -> object:
        return await ctx.call(
            ReplayCall(
                SemanticKey("guarded.read"),
                StringValue(value="root"),
                "read.v1",
                replay.ReplayPolicy(reply_type=core_common.Identity.desc().type_name),
                core_common.Identity,
            )
        )

    group = ctx.group()
    group.start(physical_root)
    group.start(read)
    await group.all()
    return "completed"


@dataclass
class GuardedRead:
    """The owner may rebind backing during inspect, so it needs durable proof first."""

    owner: FakeReplayExecutor
    registry: WriterRegistry
    store: FakeStateStore
    inspected: list[str] = field(default_factory=list)

    def check(self) -> None:
        proof = self.registry.require_proof()
        stored = self.store.load()
        assert isinstance(stored, StoredEnvelope)
        durable = ReplayWriterRecovery.from_records(Records.from_binary(stored.payload).records)
        assert durable.require(self.registry.backing).proof() == proof

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        self.check()
        return await self.owner.execute(span, fence)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        self.check()
        self.inspected.append(span.id)
        return await self.owner.inspect(span, fence)


async def test_proof_and_retirement_precede_inspection_that_can_rebind_backing(
    tmp_path: Path, sim: Sim
) -> None:
    store = FakeStateStore()

    def perform(span: Span) -> ReplayResult:
        assert span.replay_physical is not None
        if span.replay_physical.ordinal == 0:
            problem = "lost provider reply"
            raise ConnectionError(problem)
        return ReplayResult(core_common.Identity(root="answer"))

    writer = FakeReplayExecutor(perform)
    first = host(store, program, writer, sim)
    first.runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    spans = first.runtime.prepare(now=sim.clock.now(), at=Timestamp())
    (old,) = [span for span in spans if span.replay_physical is not None]
    (read,) = [span for span in spans if span.replay_physical is None]
    barrier = proof_barrier(Project.open(tmp_path), old)
    try:
        with pytest.raises(ConnectionError):
            await first.runtime.dispatch(old.id, writer, now=sim.clock.now())
    finally:
        first.runtime.close(now=sim.clock.now())
    reader = GuardedRead(
        FakeReplayExecutor(lambda _: ReplayResult(core_common.Identity(root="read"))),
        barrier.registry,
        store,
    )
    owners = ReplayExecutors({core_common.Identity: writer, StringValue: reader})
    resumed = replace(host(store, program, owners, sim), writer_barrier=barrier)
    assert await run_replay(resumed, launch("resume")) == ReplayCompleted("completed")
    assert reader.inspected == [read.id]
    assert resumed.runtime.archive.span(old.id).has_field("ended_at")
    assert len(writer.invocations) == 2
    assert resumed.runtime.archive.unresolved == ()
