"""Persisted physical metadata fails closed before it can authorize more work."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayArchive, ReplayError, ReplayRootReuse, ReplayWriterRecovery
from vs_project.api import FakeStateStore, Project
from vs_proto.api import Change, Notice, Record, SpanRecord, ValidationError, core_common, replay
from vs_proto.api import ReplayStore as Records
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayResult,
    ReplayWriterBlockedError,
    WriterProofError,
    WriterRegistry,
)
from vs_sim.api.testing import ManualClock, SimThreads

from .test_replay_physical import proof_barrier, shell
from .test_writer_reconciliation import BACKING

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StoreFence
    from vs_proto.api import Span


@pytest.mark.parametrize(
    "field",
    [
        "external_id",
        "host_incarnation",
        "deadline",
        "ordinal",
        "parent",
        "logical",
        "withdrawn_sequence",
        "observed_sequence",
        "ended_at",
        "remove_physical",
        "remove_replay",
        "policy",
    ],
)
def test_invalid_physical_updates_never_enter_the_committed_prefix(field: str) -> None:
    runtime = shell(FakeStateStore(), "first")
    try:
        (child,) = runtime.prepare(now=0, at=Timestamp())
        candidate = deepcopy(child)
        physical = candidate.replay_physical
        assert physical is not None
        sequence = runtime.archive.sequence + 1
        if field in {"external_id", "host_incarnation", "logical"}:
            setattr(physical, field, "other")
        elif field in {"deadline", "ordinal", "withdrawn_sequence", "observed_sequence"}:
            setattr(physical, field, 100)
        elif field == "ended_at":
            candidate.ended_at = 0
        elif field == "parent":
            candidate.parent = "other"
        elif field == "remove_physical":
            candidate.replay_physical = None
        elif field == "remove_replay":
            candidate.replay = None
        else:
            assert candidate.replay is not None
            assert candidate.replay.policy is not None
            candidate.replay.policy.content = b"changed"
        prefix = runtime.archive.encode()
        record = Record(
            seq=sequence,
            epoch=runtime.archive.epoch,
            clock=0,
            at=Timestamp(),
            body=Oneof("span", SpanRecord(change=Change.UPDATED, span=candidate)),
        )
        with pytest.raises((ReplayError, ValidationError)):
            runtime.archive.fork().append(record)
        assert runtime.archive.encode() == prefix
    finally:
        runtime.close(now=0)


@pytest.mark.parametrize("wrong", ["run", "host", "child", "uncommitted"])
async def test_retirement_requires_exact_committed_environment_coverage(
    tmp_path: Path, wrong: str
) -> None:
    runtime = shell(FakeStateStore(), "first")
    try:
        (child,) = runtime.prepare(now=0, at=Timestamp())
        runtime.withdraw(child.id, now=0, at=Timestamp())
        covered = deepcopy(child)
        assert covered.replay_physical is not None
        if wrong == "host":
            covered.replay_physical.host_incarnation = "host-other/1"
        elif wrong == "child":
            covered.id = "other-child"
        barrier = proof_barrier(
            Project.open(tmp_path), covered, run_id="other-run" if wrong == "run" else "run-a"
        )
        manifest = barrier.registry.close()
        result = barrier.reconciler.reconcile_writers(manifest)
        assert result.proof is not None
        barrier.registry.record(result)
        if wrong != "uncommitted":
            runtime.writer_intent(manifest, now=0, at=Timestamp())
            runtime.writer_observation(result, now=0, at=Timestamp())
        executor = FakeReplayExecutor(lambda _: ReplayResult(core_common.Identity(root="answer")))
        with pytest.raises(ReplayError, match=r"proof|cover"):
            await runtime.retire(child.id, executor, barrier.registry, clock=ManualClock())
        retained = runtime.archive.span(child.id)
        assert not retained.has_field("ended_at")
        assert retained.replay_physical is not None
        assert not retained.replay_physical.has_field("settlement_sequence")
    finally:
        runtime.close(now=0)


def notice(value: replay.WriterManifest | replay.WriterReconciliation) -> Record:
    return Record(
        body=Oneof(
            "notice", Notice(detail=Any(type_url=value.desc().type_name, value=value.to_binary()))
        )
    )


def test_writer_prefix_rejects_unordered_conflicting_and_unknown_evidence() -> None:
    manifest = replay.WriterManifest(backing=BACKING, closed=True)
    result = replay.WriterReconciliation(
        manifest=manifest,
        status=replay.WriterReconciliationStatus.PROVEN_STOPPED,
        proof=replay.WriterTerminationProof(manifest=manifest),
    )
    with pytest.raises(ReplayError, match="intent"):
        ReplayWriterRecovery.from_records([notice(result)])
    changed = deepcopy(manifest)
    changed.writers.append(
        replay.WriterIdentity(
            run_id="run", host_incarnation="host/1", physical_id="child", domain_id="domain"
        )
    )
    with pytest.raises(ReplayError, match="cannot change"):
        ReplayWriterRecovery.from_records([notice(manifest), notice(changed)])
    mismatched = deepcopy(result)
    assert mismatched.proof is not None
    mismatched.proof.manifest = changed
    mismatched.proof.absent_domains = ["domain"]
    with pytest.raises(ValidationError):
        ReplayWriterRecovery.from_records([notice(manifest), notice(mismatched)])
    assert result.proof is not None
    # Unknown field 100: preserving it in the generated decoder cannot grant reuse authority.
    with pytest.raises(ReplayError, match="unknown"):
        ReplayRootReuse(result.proof.to_binary() + b"\xa0\x06\x01").proof()


async def test_stale_registry_blocks_with_durable_identities_and_can_rebind_current_head(
    tmp_path: Path,
) -> None:
    runtime = shell(FakeStateStore(), "first")
    try:
        (child,) = runtime.prepare(now=0, at=Timestamp())
        runtime.withdraw(child.id, now=0, at=Timestamp())
        project = Project.open(tmp_path)
        old = proof_barrier(project, child)
        await old.reconcile(runtime, clock=ManualClock())
        backing = deepcopy(BACKING)
        backing.generation += 1
        current = WriterRegistry(
            project.state.state_store_namespace("writers"), backing, SimThreads()
        )
        current.create()
        with pytest.raises(ReplayWriterBlockedError, match="another active") as blocked:
            await old.reconcile(runtime, clock=ManualClock())
        manifest = blocked.value.reconciliation.manifest
        assert manifest is not None
        assert manifest.writers[0].physical_id == child.id
        stored = ReplayWriterRecovery.from_records(
            Records.from_binary(runtime.archive.encode()).records
        )
        with pytest.raises(ReplayError, match="another active"):
            stored.require(BACKING)
        active = replace(old, registry=current)
        await active.reconcile(runtime, clock=ManualClock())
        executor = FakeReplayExecutor(lambda _: ReplayResult(core_common.Identity(root="answer")))
        await runtime.retire(child.id, executor, current, clock=ManualClock())
        assert runtime.archive.span(child.id).has_field("ended_at")
    finally:
        runtime.close(now=0)


def test_incomplete_intent_can_gain_evidence_but_closed_intent_cannot_change() -> None:
    unknown = replay.WriterManifest(backing=BACKING)
    closed = deepcopy(unknown)
    closed.closed = True
    recovery = ReplayWriterRecovery.from_records([notice(unknown), notice(closed)])
    assert recovery.intent(BACKING) == closed
    with pytest.raises(ReplayError, match="cannot change"):
        ReplayWriterRecovery.from_records([notice(closed), notice(unknown)])


async def test_retirement_excludes_new_grants_and_keeps_historical_proof_for_late_usage(
    tmp_path: Path,
) -> None:
    runtime = shell(FakeStateStore(), "first")
    try:
        (child,) = runtime.prepare(now=0, at=Timestamp())
        runtime.withdraw(child.id, now=0, at=Timestamp())
        project = Project.open(tmp_path)
        barrier = proof_barrier(project, child)
        await barrier.reconcile(runtime, clock=ManualClock())
        backing = deepcopy(BACKING)
        backing.generation += 1
        successor = WriterRegistry(
            project.state.state_store_namespace("writers"), backing, SimThreads()
        )
        executor = FakeReplayExecutor(lambda _: ReplayResult(core_common.Identity(root="answer")))

        class CheckedRetirement:
            async def retire(self, span: Span, fence: StoreFence, reuse: ReplayRootReuse) -> None:
                with pytest.raises(WriterProofError, match="held by another"):
                    successor.create()
                await executor.retire(span, fence, reuse)

        await runtime.retire(child.id, CheckedRetirement(), barrier.registry, clock=ManualClock())
        successor.create()
        manifest = barrier.registry.backing
        previous = ReplayWriterRecovery.from_records(
            Records.from_binary(runtime.archive.encode()).records
        ).intent(manifest)
        assert previous is not None
        runtime.writer_observation(
            replay.WriterReconciliation(
                manifest=previous,
                status=replay.WriterReconciliationStatus.UNKNOWN,
                detail="owning service unavailable",
            ),
            now=0,
            at=Timestamp(),
        )
        runtime.observe(
            child.id,
            ReplayResult(core_common.Identity(root="late"), paid=True, cost_usd=0.25),
            now=0,
            at=Timestamp(),
        )
        restored = ReplayArchive.decode(runtime.archive.encode())
        assert restored.span(child.id).replay_observation is not None
        assert runtime.accounting.reported_cost_usd == 0.25
        assert runtime.accounting.paid_operations == 1
    finally:
        runtime.close(now=0)
