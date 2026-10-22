"""Every awaited recovery owner keeps exclusion and timestamps its completed work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import pytest

from vs_project.api import FakeStateStore, Project
from vs_proto.api import ReplayStore, core_common, replay
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayAbsent,
    ReplayCompleted,
    ReplayPending,
    ReplayResult,
    ReplayUnknown,
    drive_replay,
)
from vs_sim.api.testing import GatedBlockingRunner

from .test_replay_physical import physical_root, proof_barrier
from .test_replay_run import host, launch

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api.replay import ReplayRootReuse
    from vs_project.api import StoreFence
    from vs_proto.api import Record, Span
    from vs_sim.api.testing import Sim


Phase = Literal["inspection", "reconciliation", "retirement"]


@dataclass
class GatedRecovery:
    owner: FakeReplayExecutor
    runner: GatedBlockingRunner
    phase: Phase

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        return await self.owner.execute(span, fence)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        if self.phase == "inspection":
            await self.runner.run(lambda: None)
        return await self.owner.inspect(span, fence)

    async def retire(self, span: Span, fence: StoreFence, reuse: ReplayRootReuse) -> None:
        if self.phase == "retirement":
            await self.runner.run(lambda: None)
        await self.owner.retire(span, fence, reuse)


@pytest.mark.regression
@pytest.mark.parametrize("phase", ["inspection", "reconciliation", "retirement"])
@pytest.mark.parametrize("delay", [0, 1, 4, 7.5])
async def test_recovery_owners_renew_exclusion_and_commit_after_await(
    tmp_path: Path,
    sim: Sim,
    phase: Phase,
    delay: float,
) -> None:
    store = FakeStateStore()

    def perform(span: Span) -> ReplayResult:
        assert span.replay_physical is not None
        if span.replay_physical.ordinal == 0:
            problem = "lost provider reply"
            raise ConnectionError(problem)
        return ReplayResult(core_common.Identity(root="answer"))

    owner = FakeReplayExecutor(perform)
    first = host(store, physical_root, owner, sim)
    with pytest.raises(ConnectionError):
        await drive_replay(first, launch("first"))
    (old,) = first.runtime.archive.unresolved
    barrier = proof_barrier(Project.open(tmp_path), old)
    runner = GatedBlockingRunner(held=True)
    if phase == "reconciliation":
        barrier = replace(barrier, blocking=runner)
    executor = GatedRecovery(owner, runner, phase)
    resumed = replace(host(store, physical_root, executor, sim), writer_barrier=barrier)
    task = asyncio.create_task(drive_replay(resumed, launch("resume")))
    try:
        await runner.wait_in_flight(1, task)
        before = resumed.runtime.archive.sequence
        await sim.clock.sleep(delay)
        completed_at = sim.clock.now()
        contender = store.acquire("host-b", completed_at, 3)
        assert contender is None, "recovery lost exclusion while its owner was awaiting"
        runner.release()
        assert await task == ReplayCompleted("answer")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    records = ReplayStore.from_binary(resumed.runtime.archive.encode()).records
    completion_records = [record for record in records if is_recovery_completion(record, old.id)]
    assert len(completion_records) == 2
    for record in completion_records:
        assert record.at is not None
        if record.seq > before:
            assert record.at.seconds + record.at.nanos / 1_000_000_000 >= completed_at
    assert resumed.runtime.archive.unresolved == ()


def is_recovery_completion(record: Record, child_id: str) -> bool:
    body = record.body
    if body is None:
        return False
    if body.field == "notice" and body.value.detail is not None:
        return body.value.detail.type_url == replay.WriterReconciliation.desc().type_name
    if body.field == "span" and body.value.span is not None:
        span = body.value.span
        return span.id == child_id and span.has_field("ended_at")
    return False
