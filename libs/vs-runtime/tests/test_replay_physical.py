"""Physical execution evidence survives crashes without changing semantic identity."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import (
    ReplayCall,
    ReplayError,
    ReplayFlow,
    ReplayWriterRecovery,
    SemanticKey,
)
from vs_project.api import FakeStateStore, Project
from vs_proto.api import ReplayStore as Records
from vs_proto.api import TokenUsage, core_common, replay
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCompleted,
    ReplayRecoveryRequiredError,
    ReplayResult,
    ReplayRuntime,
    ReplayUnknown,
    ReplayWriterBarrier,
    UnsupportedWriterReconciler,
    WriterRegistry,
    run_replay,
)
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import DISCARD
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    InlineBlockingRunner,
    ManualClock,
    Sim,
    SimThreads,
)

from .test_replay_run import host, launch
from .test_writer_reconciliation import BACKING, owner

if TYPE_CHECKING:
    from pathlib import Path

    from vs_proto.api import Span


async def physical_root(ctx: ReplayFlow) -> object:
    return (
        await ctx.call(
            ReplayCall(
                SemanticKey("retryable"),
                core_common.Identity(root="retained-input"),
                "physical.v1",
                replay.ReplayPolicy(
                    reply_type=core_common.Identity.desc().type_name,
                    physical_within_seconds=30,
                    reservation=replay.ReplayResources(money_usd=1),
                ),
                core_common.Identity,
            )
        )
    ).root


def shell(store: FakeStateStore, request: str) -> ReplayRuntime:
    runtime = ReplayRuntime(store, physical_root, replay.ReplayAllowance())
    runtime.start(launch(request), now=0, at=Timestamp())
    return runtime


def proof_barrier(project: Project, child: Span, *, run_id: str = "run-a") -> ReplayWriterBarrier:
    engine = FakeDockerEngine(launcher=FakePipedProcessLauncher())
    domain = engine.run(
        ["docker", "run", "-d", "image"], timeout_seconds=1, output=DISCARD
    ).stdout.strip()
    ledger = WriterRegistry(project.state.state_store_namespace("writers"), BACKING, SimThreads())
    ledger.create()
    assert child.replay_physical is not None
    ledger.grant(
        replay.WriterIdentity(
            run_id=run_id,
            host_incarnation=child.replay_physical.host_incarnation,
            physical_id=child.id,
            domain_id=domain,
        )
    )
    return ReplayWriterBarrier(ledger, owner(engine), InlineBlockingRunner())


@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
async def test_physical_result_crash_prefix_accepts_one_parent(sim: Sim, boundary: str) -> None:
    store = FakeStateStore()
    executor = FakeReplayExecutor(
        lambda _: ReplayResult(core_common.Identity(root="answer"), paid=True, cost_usd=0.25)
    )
    first = shell(store, "first")
    (child,) = first.prepare(now=0, at=Timestamp())
    assert child.replay_physical is not None
    if boundary != "prepared":
        result = await first.dispatch(child.id, executor, now=0)
        if boundary != "executed":
            first.observe(child.id, result, now=0, at=Timestamp())
            if boundary == "accepted":
                first.accept(child.id, now=0, at=Timestamp())
    first.close(now=0)
    resumed = host(store, physical_root, executor, sim)
    assert await run_replay(resumed, launch("resume")) == ReplayCompleted("answer")
    assert len(executor.invocations) == 1
    assert len(resumed.runtime.archive.history) == 1
    accepted = [
        span
        for span in resumed.runtime.archive.spans
        if span.replay is not None and span.replay.outcome is not None
    ]
    assert len(accepted) == 1
    assert accepted[0].replay is not None
    assert accepted[0].replay.accepted_child == executor.invocations[0]
    assert resumed.runtime.accounting.paid_operations == 1
    assert resumed.runtime.accounting.reported_cost_usd == 0.25


async def test_unknown_child_requires_proof_and_replacement_keeps_parent_and_deadline(
    tmp_path: Path,
) -> None:
    store = FakeStateStore()

    async def fail(_: Span) -> ReplayResult:
        problem = "lost reply"
        raise ConnectionError(problem)

    executor = FakeReplayExecutor(fail)
    first = shell(store, "first")
    (old,) = first.prepare(now=0, at=Timestamp())
    barrier = proof_barrier(Project.open(tmp_path), old)
    with pytest.raises(ConnectionError):
        await first.dispatch(old.id, executor, now=0)
    first.close(now=0)
    resumed = shell(store, "resume")
    resumed.withdraw(old.id, now=0, at=Timestamp())
    with pytest.raises(ReplayRecoveryRequiredError):
        resumed.prepare(now=0, at=Timestamp())
    assert not resumed.archive.span(old.id).has_field("ended_at")
    await barrier.reconcile(resumed, clock=ManualClock())
    await resumed.retire(old.id, executor, barrier.registry, clock=ManualClock())
    resumed.finish_recovery()
    (new,) = resumed.prepare(now=1, at=Timestamp(seconds=1))
    assert old.replay_physical is not None
    assert new.replay_physical is not None
    assert old.parent == new.parent
    assert old.replay_physical.ordinal == 0
    assert new.replay_physical.ordinal == 1
    assert old.id != new.id
    assert old.replay_physical.host_incarnation != new.replay_physical.host_incarnation
    assert old.replay_physical.deadline == new.replay_physical.deadline == 30
    assert old.replay is not None
    assert new.replay is not None
    assert old.replay.identity == new.replay.identity
    assert old.replay.policy == new.replay.policy
    resumed.observe(
        old.id,
        ReplayResult(core_common.Identity(root="late"), paid=True, cost_usd=0.2),
        now=1,
        at=Timestamp(seconds=1),
    )
    winner = FakeReplayExecutor(
        lambda _: ReplayResult(core_common.Identity(root="answer"), paid=True, cost_usd=0.3)
    )
    result = await resumed.dispatch(new.id, winner, now=1)
    resumed.observe(new.id, result, now=1, at=Timestamp(seconds=1))
    resumed.accept(new.id, now=1, at=Timestamp(seconds=1))
    assert resumed.result == "answer"
    assert resumed.accounting.paid_operations == 2
    assert resumed.accounting.reported_cost_usd == pytest.approx(0.5)
    accepted = resumed.archive.span(old.parent)
    assert accepted.replay is not None
    assert accepted.replay.accepted_child == new.id
    resumed.close(now=1)
    fence = store.acquire("host-b", 1, 3)
    assert fence is not None
    with pytest.raises(ValueError, match="withdrawn"):
        await executor.execute(resumed.archive.span(old.id), fence)
    store.release(fence, 1)


@pytest.mark.regression
@example(costs=[0], copies=1)
@given(
    costs=st.lists(st.integers(min_value=0, max_value=10), min_size=1, max_size=4),
    copies=st.integers(min_value=1, max_value=4),
)
def test_superseded_observations_retain_usage_once_and_never_win(
    costs: list[int], copies: int
) -> None:
    store = FakeStateStore()
    runtime = shell(store, "first")
    (child,) = runtime.prepare(now=0, at=Timestamp())
    runtime.withdraw(child.id, now=0, at=Timestamp())
    result = ReplayResult(
        core_common.Identity(root="loser"),
        paid=True,
        cost_usd=sum(costs),
        usage=TokenUsage(input=sum(costs), output=2),
    )
    for _ in range(copies):
        runtime.observe(child.id, result, now=0, at=Timestamp())
    assert runtime.archive.pending_observation is None
    assert runtime.archive.history == ()
    assert runtime.accounting.paid_operations == 1
    assert runtime.accounting.reported_cost_usd == sum(costs)
    assert runtime.accounting.resources.reported.input_tokens == sum(costs)
    with pytest.raises(ReplayError, match="first eligible"):
        runtime.accept(child.id, now=0, at=Timestamp())
    contradictory = replace(result, cost_usd=sum(costs) + 1)
    with pytest.raises(ReplayError, match="replaced"):
        runtime.observe(child.id, contradictory, now=0, at=Timestamp())
    runtime.close(now=0)
    restored = shell(store, "resume")
    assert restored.accounting.paid_operations == 1
    assert restored.accounting.reported_cost_usd == sum(costs)
    assert restored.recovery[0].id == child.id
    restored.close(now=0)


async def test_blocked_recovery_persists_unknown_manifest_without_dispatch(
    tmp_path: Path, sim: Sim
) -> None:
    store = FakeStateStore()
    first = shell(store, "first")
    first.prepare(now=0, at=Timestamp())
    first.close(now=0)
    executor = FakeReplayExecutor(lambda _: ReplayResult(core_common.Identity(root="answer")))
    ledger = WriterRegistry(
        Project.open(tmp_path).state.state_store_namespace("missing"), BACKING, SimThreads()
    )
    barrier = ReplayWriterBarrier(
        ledger, UnsupportedWriterReconciler("host"), InlineBlockingRunner()
    )
    resumed = replace(host(store, physical_root, executor, sim), writer_barrier=barrier)
    with pytest.raises(ValueError, match="Recovery blocked"):
        await run_replay(resumed, launch("resume"))
    assert executor.invocations == ()
    recovery = ReplayWriterRecovery.from_records(
        Records.from_binary(resumed.runtime.archive.encode()).records
    )
    assert len(recovery.results) == 1
    with pytest.raises(ReplayError, match="absent"):
        recovery.require(BACKING)


@pytest.mark.regression
@pytest.mark.parametrize("credit", [0.5, 2])
@pytest.mark.parametrize("within", [None, 5])
async def test_replacement_retains_original_policy_and_reservation_after_edit(
    tmp_path: Path, credit: float, within: float | None
) -> None:
    async def edited(ctx: ReplayFlow) -> object:
        return await ctx.call(
            ReplayCall(
                SemanticKey("retryable"),
                core_common.Identity(root="retained-input"),
                "physical.v1",
                replay.ReplayPolicy(
                    content=b"edited",
                    reply_type=core_common.Identity.desc().type_name,
                    physical_within_seconds=within,
                    reservation=replay.ReplayResources(money_usd=0),
                ),
                core_common.Identity,
            )
        )

    async def failed(_: Span) -> ReplayResult:
        problem = "provider lost acceptance"
        raise ConnectionError(problem)

    store = FakeStateStore()
    executor = FakeReplayExecutor(failed)
    first = shell(store, "first")
    (old,) = first.prepare(now=0, at=Timestamp())
    barrier = proof_barrier(Project.open(tmp_path), old)
    with pytest.raises(ConnectionError):
        await first.dispatch(old.id, executor, now=0)
    first.close(now=0)
    resumed = ReplayRuntime(
        store, edited, replay.ReplayAllowance(resources=replay.ReplayResources(money_usd=credit))
    )
    resumed.start(launch("resume"), now=0, at=Timestamp())
    try:
        assert isinstance(await resumed.inspect(old.id, executor, now=0), ReplayUnknown)
        resumed.withdraw(old.id, now=0, at=Timestamp())
        await barrier.reconcile(resumed, clock=ManualClock())
        await resumed.retire(old.id, executor, barrier.registry, clock=ManualClock())
        resumed.finish_recovery()
        prepared = resumed.prepare(now=0, at=Timestamp())
        if credit < 1:
            assert prepared == ()
            assert resumed.admission_limited
        else:
            (new,) = prepared
            assert new.replay is not None
            assert old.replay is not None
            assert new.replay.policy == old.replay.policy
            assert new.replay_physical is not None
            assert new.replay_physical.ordinal == 1
            assert new.replay_physical.deadline == 30
            assert resumed.accounting.resources.committed.money_usd == 1
    finally:
        resumed.close(now=0)
