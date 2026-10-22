"""W1-W14 faults cross public owner boundaries, including crashes during recovery."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

import pytest
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayArchive, ReplayWriterRecovery
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import FakeStateStore, Project, StoredEnvelope
from vs_proto.api import Change, ReplayStore, SpanStatus, core_common, replay
from vs_runtime.api.replay import (
    DockerWriterReconciler,
    FakeReplayExecutor,
    ReplayAbsent,
    ReplayCompleted,
    ReplayPending,
    ReplayResult,
    ReplayRuntime,
    ReplayUnknown,
    WriterRegistry,
    run_replay,
)
from vs_sim.api.testing import SimThreads

from .test_replay_physical import physical_root, proof_barrier
from .test_replay_run import host, launch

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api.replay import ReplayRootReuse
    from vs_project.api import CommitOutcome, StoreFence
    from vs_proto.api import Span
    from vs_runtime.api.replay import ReplayWriterBarrier
    from vs_sim.api.testing import Sim


class CrossingStore(FakeStateStore):
    """Retain the real Fake store's CAS/lease behavior while losing selected replies."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = FaultGate(FaultPlan(seed=0))

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        return self.gate.around(
            Boundary.EXECUTOR_REQUEST, "W1", partial(super().acquire, host_id, now, duration)
        )

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        record = ReplayStore.from_binary(envelope.payload).records[-1]
        row = "other"
        body = record.body
        if body is not None and body.field == "notice" and body.value.detail is not None:
            name = body.value.detail.type_url
            if name == replay.WriterManifest.desc().type_name:
                row = "W2"
            elif name == replay.WriterReconciliation.desc().type_name:
                row = "W7"
        if body is not None and body.field == "span" and body.value.span is not None:
            span = body.value.span
            physical = span.replay_physical
            if physical is not None:
                row = _physical_row(span, record.seq, body.value.change)
            elif span.replay is not None and span.replay.accepted_sequence == record.seq:
                row = "W13"
        return self.gate.around(
            Boundary.DURABLE_WRITE,
            row,
            partial(super().commit, expected_revision, envelope, fence, now),
        )


def _physical_row(span: Span, sequence: int, change: Change) -> str:
    physical = span.replay_physical
    assert physical is not None
    if physical.withdrawn_sequence == sequence:
        return "W4"
    if physical.settlement_sequence == sequence:
        return "W8a"
    if physical.observed_sequence == sequence:
        return "W12"
    return "W10" if change is Change.STARTED else "other"


@dataclass
class CrossingExecutor:
    owner: FakeReplayExecutor
    gate: FaultGate

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        return await self.gate.around_async(
            Boundary.EXECUTOR_REQUEST, "W11", partial(self.owner.execute, span, fence)
        )

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        return await self.gate.around_async(
            Boundary.EXECUTOR_REQUEST, "W3", partial(self.owner.inspect, span, fence)
        )

    async def retire(self, span: Span, fence: StoreFence, reuse: ReplayRootReuse) -> None:
        await self.gate.around_async(
            Boundary.EXECUTOR_REQUEST, "W8b", partial(self.owner.retire, span, fence, reuse)
        )


@dataclass
class CrossingReconciler:
    owner: DockerWriterReconciler
    gate: FaultGate

    def reconcile_writers(self, manifest: replay.WriterManifest) -> replay.WriterReconciliation:
        return self.gate.around(
            Boundary.EXECUTOR_REQUEST, "W6", partial(self.owner.reconcile_writers, manifest)
        )


@pytest.mark.parametrize(
    ("row", "advance_generation"),
    [
        *[
            (row, False)
            for row in (
                "W1",
                "W2",
                "W3",
                "W4",
                "W5",
                "W6",
                "W7",
                "W8a",
                "W8b",
                "W10",
                "W11",
                "W12",
                "W13",
                "W14",
            )
        ],
        ("W8a", True),
        ("W8b", True),
    ],
)
async def test_recovery_crash_rows_preserve_identity_proof_usage_and_winner(
    tmp_path: Path, sim: Sim, row: str, *, advance_generation: bool
) -> None:
    store = CrossingStore()
    baseline: ReplayWriterBarrier | None = None

    def perform(span: Span) -> ReplayResult:
        nonlocal baseline
        assert span.replay_physical is not None
        if span.replay_physical.ordinal == 0:
            baseline = proof_barrier(Project.open(tmp_path), span)
            problem = "provider lost acceptance"
            raise ConnectionError(problem)
        return ReplayResult(core_common.Identity(root="answer"), paid=True, cost_usd=0.5)

    owner = FakeReplayExecutor(perform)
    first = host(store, physical_root, owner, sim)
    with pytest.raises(ConnectionError):
        await run_replay(first, launch("first"))
    (old,) = first.runtime.archive.unresolved
    assert baseline is not None
    gate = FaultGate(
        FaultPlan(
            seed=0,
            rules=(
                FaultRule(
                    boundary=Boundary.DURABLE_WRITE
                    if row in {"W2", "W4", "W7", "W8a", "W10", "W12", "W13"}
                    else Boundary.EXECUTOR_REQUEST,
                    target=row,
                    at=1,
                    fault=HostFault.CRASH_AFTER,
                ),
            ),
        )
    )
    store.gate = gate
    executor = CrossingExecutor(owner, gate)
    reconciler = baseline.reconciler
    assert isinstance(reconciler, DockerWriterReconciler)
    reaping = replace(
        reconciler,
        reap=lambda domains: gate.around(
            Boundary.EXECUTOR_REQUEST, "W5", partial(reconciler.reap, domains)
        ),
    )
    barrier = replace(baseline, reconciler=CrossingReconciler(reaping, gate))

    async def run(request: str) -> object:
        binding = replace(host(store, physical_root, executor, sim), writer_barrier=barrier)
        return await gate.around_async(
            Boundary.EXECUTOR_REQUEST, "W14", partial(run_replay, binding, launch(request))
        )

    with pytest.raises(HostCrashError, match=row):
        await run("crash")
    gate.heal()
    historical = None
    if advance_generation:
        barrier, historical = next_generation(store, barrier, Project.open(tmp_path), old.id)
    # A crash immediately after acquiring exclusion may lose its lease response.
    await sim.clock.sleep(4)
    assert await run("healed") == ReplayCompleted("answer")
    final = ReplayRuntime(store, physical_root, replay.ReplayAllowance())
    final.start(launch("inspect"), now=sim.clock.now(), at=Timestamp())
    try:
        assert_recovered(final, old, owner, historical)
        records = ReplayStore.from_binary(final.archive.encode()).records
        reuse = ReplayWriterRecovery.from_records(records).require(barrier.registry.backing)
        assert reuse.proof() == barrier.registry.require_proof()
        crossing_rows = [call.target for call in gate.calls]
        assert "W4" in crossing_rows
        assert "W7" in crossing_rows
        assert "W8a" in crossing_rows
        assert crossing_rows.index("W4") < crossing_rows.index("W5")
        assert crossing_rows.index("W7") < crossing_rows.index("W8a")
    finally:
        final.close(now=sim.clock.now())


def assert_recovered(
    final: ReplayRuntime, old: Span, owner: FakeReplayExecutor, historical: bytes | None
) -> None:
    children = final.archive.children(old.parent)
    assert len(children) in {2, 3}  # A lost W10 commit creates one positively absent child.
    assert len(owner.invocations) == 2
    assert owner.invocations[0] == old.id
    assert children[0].replay_physical is not None
    assert children[0].replay_physical.has_field("withdrawn_sequence")
    assert children[0].replay_physical.has_field("settlement_sequence")
    if historical is not None:
        assert children[0].replay_physical.termination_proof_digest == historical
    assert children[0].status is SpanStatus.STOPPED
    assert len(final.archive.history) == 1
    assert final.accounting.paid_operations == 1
    assert final.accounting.reported_cost_usd == 0.5
    assert final.archive.unresolved == ()


def next_generation(
    store: CrossingStore, barrier: ReplayWriterBarrier, project: Project, child_id: str
) -> tuple[ReplayWriterBarrier, bytes]:
    stored = store.load()
    assert isinstance(stored, StoredEnvelope)
    retained = ReplayArchive.decode(stored.payload).span(child_id)
    assert retained.replay_physical is not None
    historical = retained.replay_physical.termination_proof_digest
    assert historical
    backing = deepcopy(barrier.registry.backing)
    backing.generation += 1
    successor = WriterRegistry(
        project.state.state_store_namespace("writers"), backing, SimThreads()
    )
    successor.create()
    return replace(barrier, registry=successor), historical
