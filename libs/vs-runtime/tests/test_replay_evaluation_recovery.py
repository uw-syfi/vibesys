"""An empty replacement client is not proof that a submitted evaluation never began."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from tests.support.session_world import CrashOnReplace

from vs_core.api import RequestId
from vs_core.api.replay import SemanticKey, classify_context
from vs_evaluation.api import PollPhase
from vs_evaluation.api.testing import FakeEvaluationExecutor, InMemoryEvaluationNamespace
from vs_project.api import FakeStateStore, Project
from vs_proto.api import EpochStarted, core_vocabulary, replay
from vs_proto.api import replay_evaluation as pb
from vs_runtime.api import EvaluationRetries, RetryingEvaluationExecutor
from vs_runtime.api.core import ReceiptStore, handle_for
from vs_runtime.api.replay import (
    EvaluationReplayExecutor,
    ReplayCompleted,
    ReplayLaunch,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayResult,
    ReplayRuntime,
    ReplayUnknown,
    replay_measurement_plan,
    run_replay,
)
from vs_runtime.api.slurm_evaluation_stack import plan
from vs_sim.api.testing import ManualClock, SimThreads, yield_until

from .test_evaluation_executor_contract import _local, _Script
from .test_local_evaluation_receipts import executor as durable_local
from .test_replay_evaluation import SCOPE, program
from .test_replay_run import host
from .test_retrying_evaluation import Outcome, _terminal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_evaluation.api import RecoverableEvaluationExecutor
    from vs_proto.api import Span
    from vs_runtime.api.replay import ReplayExecutor
    from vs_sim.api.testing import Sim

    type Factory = Callable[..., ReplayExecutor]


def control_span(original: Span, action: Oneof) -> Span:
    span = deepcopy(original)
    span.id += "/" + action.field
    assert span.replay is not None
    assert span.replay.policy is not None
    assert span.replay.policy.request is not None
    context = classify_context(
        pb.ReplayEvaluationRequest(
            scope=SCOPE, admission="admit-1", within_seconds=100, action=action
        ),
        contract="measurement.v1",
    )
    span.replay.key = SemanticKey("evaluation." + action.field).item("round-1").to_proto()
    span.replay.identity = context.identity.canonical
    span.replay.identity_digest = context.identity.digest
    span.replay.dependencies = context.identity.dependencies
    span.replay.policy.request.value = context.content
    return span


async def local_recovery_case(
    tmp_path: Path, sim: Sim, implementation: Factory, *, retrying: bool
) -> None:
    old = await _local(tmp_path / "old", _Script.HOLD_FIRST_STAGE, None)
    replacement = await _local(tmp_path / "new", _Script.SUCCEED, None)
    old_jobs = old.executor
    replacement_jobs = replacement.executor
    if retrying:
        retry_state = InMemoryEvaluationNamespace()
        retries = EvaluationRetries(infrastructure_attempts=3, ambiguous_attempts=2)
        old_jobs = RetryingEvaluationExecutor(old_jobs, retries, retry_state)
        replacement_jobs = RetryingEvaluationExecutor(replacement_jobs, retries, retry_state)
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    store = FakeStateStore()
    timing = ReplayOperationHost("run", store, sim.clock)
    crashing = implementation(
        old_jobs, CrashOnReplace(namespace, threads=SimThreads()), timing, within_seconds=100
    )
    runtime = ReplayRuntime(
        store,
        program(replay_measurement_plan(plan(candidate=old.snapshot))),
        replay.ReplayAllowance(),
    )
    try:
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
        with pytest.raises(SystemExit):
            await runtime.dispatch(span.id, crashing, now=sim.clock.now())
        handle = handle_for(RequestId(root=span.id))
        await yield_until(
            lambda: old.executor.poll(handle),
            lambda seen: seen.phase is PollPhase.RUNNING,
            "the old evaluation's held stage",
        )
        runtime.close(now=sim.clock.now())
        fence = store.acquire("host-a", sim.clock.now(), 100)
        assert fence is not None
        resumed = implementation(
            replacement_jobs, ReceiptStore(namespace, SimThreads()), timing, within_seconds=100
        )
        assert isinstance(await resumed.inspect(span, fence), ReplayUnknown)
        assert not replacement.workspaces.candidates
        cancel = control_span(span, Oneof("cancel", pb.ReplayCancelMeasurement(resource=handle)))
        with pytest.raises(ReplayOperationError, match="remains unknown"):
            await resumed.execute(cancel, fence)
        closed = await resumed.execute(
            control_span(span, Oneof("close", pb.ReplayCloseMeasurements())), fence
        )
        assert isinstance(closed, ReplayResult)
        assert isinstance(closed.outcome, pb.ReplayEvaluationResult)
        assert not closed.outcome.released
        assert handle in closed.outcome.children
        assert (await old.executor.poll(handle)).phase is PollPhase.RUNNING
        assert not replacement.workspaces.candidates
        assert store.release(fence, sim.clock.now())
    finally:
        await replacement.executor.close()
        await old.executor.close()


@pytest.mark.regression
@pytest.mark.parametrize("retrying", [False, True])
async def test_empty_new_local_client_neither_repeats_nor_claims_to_cancel_old_work(
    tmp_path: Path, sim: Sim, *, retrying: bool
) -> None:
    await local_recovery_case(tmp_path, sim, EvaluationReplayExecutor, retrying=retrying)


@pytest.mark.regression
async def test_first_native_submission_observes_failure_without_dispatching_a_hidden_retry(
    tmp_path: Path, sim: Sim
) -> None:
    inner = FakeEvaluationExecutor(ManualClock())
    inner.script_observations(_terminal(Outcome.INFRASTRUCTURE))
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    jobs = RetryingEvaluationExecutor(
        inner,
        EvaluationRetries(infrastructure_attempts=3, ambiguous_attempts=2),
        namespace,
    )
    store = FakeStateStore()
    runtime = ReplayRuntime(
        store, program(replay_measurement_plan(plan())), replay.ReplayAllowance()
    )
    owner = EvaluationReplayExecutor(
        jobs,
        ReceiptStore(namespace, SimThreads()),
        ReplayOperationHost("run", store, sim.clock),
        within_seconds=100,
    )
    try:
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
        result = await runtime.dispatch(span.id, owner, now=sim.clock.now())
        assert isinstance(result.outcome, pb.ReplayEvaluationResult)
        assert result.outcome.job is not None
        assert result.outcome.job.terminal
        assert result.outcome.job.failure is pb.ReplayMeasurementFailure.INFRASTRUCTURE
        handle = handle_for(RequestId(root=span.id))
        assert [item.handle_id for item in inner.submissions] == [handle]
        # W12/W13: a lost replay observation is reconstructed from the owner
        # receipt, without executing the failed or next physical try again.
        runtime.close(now=sim.clock.now())
        fence = store.acquire("host-a", sim.clock.now(), 100)
        assert fence is not None
        assert await owner.inspect(span, fence) == result
        assert [item.handle_id for item in inner.submissions] == [handle]
        assert store.release(fence, sim.clock.now())
    finally:
        runtime.close(now=sim.clock.now())
        await jobs.close()


async def test_completed_local_evidence_replays_through_an_empty_fresh_owner(
    tmp_path: Path, sim: Sim
) -> None:
    original = await _local(tmp_path / "old", _Script.SUCCEED, None)
    fresh = await _local(tmp_path / "new", _Script.SUCCEED, None)
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    store = FakeStateStore()
    flow = program(replay_measurement_plan(plan(candidate=original.snapshot)))

    def owner(jobs: RecoverableEvaluationExecutor) -> EvaluationReplayExecutor:
        return EvaluationReplayExecutor(
            jobs,
            ReceiptStore(namespace, SimThreads()),
            ReplayOperationHost("run", store, sim.clock),
            within_seconds=100,
        )

    try:
        completed = await run_replay(
            host(store, flow, owner(original.executor), sim),
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
        )
        assert isinstance(completed, ReplayCompleted)
        assert completed.value
        await original.executor.close()
        resumed = host(store, flow, owner(fresh.executor), sim)
        assert (
            await run_replay(
                resumed, ReplayLaunch(EpochStarted(run_id="run"), "resume", "host-a", 100)
            )
            == completed
        )
        assert resumed.runtime.accounting.paid_operations == 1
        assert not fresh.workspaces.candidates
    finally:
        await original.executor.close()
        await fresh.executor.close()


@pytest.mark.regression
async def test_sealed_local_submission_survives_lost_delivery_to_the_replay_log(
    tmp_path: Path, sim: Sim
) -> None:
    old = await _local(tmp_path / "old", _Script.HOLD_FIRST_STAGE, None)
    fresh = await _local(tmp_path / "new", _Script.SUCCEED, None)
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    store = FakeStateStore()
    timing = ReplayOperationHost("run", store, sim.clock)
    runtime = ReplayRuntime(
        store,
        program(replay_measurement_plan(plan(candidate=old.snapshot))),
        replay.ReplayAllowance(),
    )
    first = EvaluationReplayExecutor(
        old.executor, ReceiptStore(namespace, SimThreads()), timing, within_seconds=100
    )
    try:
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
        submitted = await runtime.dispatch(span.id, first, now=sim.clock.now())
        runtime.close(now=sim.clock.now())
        fence = store.acquire("host-a", sim.clock.now(), 100)
        assert fence is not None
        resumed = EvaluationReplayExecutor(
            fresh.executor, ReceiptStore(namespace, SimThreads()), timing, within_seconds=100
        )
        for _ in range(3):
            assert await resumed.inspect(span, fence) == submitted
        handle = handle_for(RequestId(root=span.id))
        assert (await fresh.executor.recover(handle)).phase is PollPhase.UNKNOWN
        assert not fresh.workspaces.candidates
        # Replaying acceptance proves neither completion nor release of the job.
        observed = await resumed.execute(
            control_span(span, Oneof("observe", pb.ReplayMeasurementJob(resource=handle))),
            fence,
        )
        assert isinstance(observed.outcome, pb.ReplayEvaluationResult)
        assert observed.outcome.job is not None
        assert observed.outcome.job.status is core_vocabulary.ObservationStatus.UNKNOWN
        assert not observed.outcome.job.terminal
        assert not observed.outcome.job.released
        assert not fresh.workspaces.candidates
        assert store.release(fence, sim.clock.now())
    finally:
        runtime.close(now=sim.clock.now())
        await fresh.executor.close()
        await old.executor.close()


@pytest.mark.regression
@pytest.mark.parametrize("retrying", [False, True])
async def test_local_stage_results_survive_crash_before_measurement_owner_seals(
    tmp_path: Path, sim: Sim, *, retrying: bool
) -> None:
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    local_state = InMemoryEvaluationNamespace()
    old, _, workspaces = durable_local(local_state)
    fresh, evaluation, replacements = durable_local(local_state)
    snapshot = await workspaces.root.snapshot("candidate")
    old_jobs: RecoverableEvaluationExecutor = old
    fresh_jobs: RecoverableEvaluationExecutor = fresh
    if retrying:
        policy = EvaluationRetries(infrastructure_attempts=3, ambiguous_attempts=2)
        old_jobs = RetryingEvaluationExecutor(old, policy, local_state)
        fresh_jobs = RetryingEvaluationExecutor(fresh, policy, local_state)
    store = FakeStateStore()
    timing = ReplayOperationHost("run", store, sim.clock)
    runtime = ReplayRuntime(
        store, program(replay_measurement_plan(plan(candidate=snapshot))), replay.ReplayAllowance()
    )
    crashing = EvaluationReplayExecutor(
        old_jobs, CrashOnReplace(namespace, SimThreads()), timing, within_seconds=100
    )
    try:
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
        with pytest.raises(SystemExit):
            await runtime.dispatch(span.id, crashing, now=sim.clock.now())
        handle = handle_for(RequestId(root=span.id))
        await yield_until(
            lambda: old.recover(handle),
            lambda p: p.phase is PollPhase.ENDED,
            "the local results before owner sealing",
        )
        runtime.close(now=sim.clock.now())
        fence = store.acquire("host-a", sim.clock.now(), 100)
        assert fence is not None
        recovered = EvaluationReplayExecutor(
            fresh_jobs, ReceiptStore(namespace, SimThreads()), timing, within_seconds=100
        )
        result = await recovered.inspect(span, fence)
        assert isinstance(result, ReplayResult)
        assert isinstance(result.outcome, pb.ReplayEvaluationResult)
        assert result.outcome.job is not None
        assert result.outcome.job.terminal
        assert result.outcome.job.released
        assert len(result.outcome.job.evidence) == 2
        assert await recovered.inspect(span, fence) == result
        assert not evaluation.accuracy_calls
        assert not replacements.candidates
        assert store.release(fence, sim.clock.now())
    finally:
        runtime.close(now=sim.clock.now())
        await old.close()
        await fresh.close()
