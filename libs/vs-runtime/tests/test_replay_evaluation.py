"""The native evaluation boundary shares recovery over both production executors."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api import RequestId
from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey, classify_context
from vs_evaluation.api import PollPhase
from vs_project.api import FakeStateStore, Project
from vs_proto.api import EpochStarted, core_common, core_vocabulary, replay
from vs_proto.api import replay_evaluation as pb
from vs_runtime.api.core import ReceiptStore, handle_for
from vs_runtime.api.executor_contracts import FaultingNamespace, ProcessKilledError
from vs_runtime.api.replay import (
    EvaluationReplayExecutor,
    ReplayCompleted,
    ReplayLaunch,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayParked,
    ReplayRecoveryRequiredError,
    ReplayRuntime,
    drive_replay,
    replay_measurement_plan,
)
from vs_runtime.api.slurm_evaluation_stack import plan
from vs_sim.api.testing import SimThreads, swept

from .test_evaluation_executor_contract import BUILDERS, _Build, _Script
from .test_replay_run import host

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from vs_project.api import StateNamespace
    from vs_sim.api.testing import Sim


SCOPE = core_common.Scope(
    owner=core_common.ScopeOwner(
        owner=Oneof("attempt", core_common.AttemptId(root="round-1", kind="attempt"))
    ),
    generation="0",
)


async def step(
    ctx: ReplayFlow, action: Oneof, *, sample: str = "once"
) -> pb.ReplayEvaluationResult:
    return await ctx.call(
        ReplayCall(
            SemanticKey("evaluation." + action.field).item("round-1").sample(sample),
            classify_context(
                pb.ReplayEvaluationRequest(
                    scope=SCOPE, admission="admit-1", within_seconds=100, action=action
                ),
                contract="measurement.v1",
            ),
            "measurement.v1",
            replay.ReplayPolicy(
                reply_type=pb.ReplayEvaluationResult.desc().type_name,
                reservation=replay.ReplayResources(money_usd=0, input_tokens=0, output_tokens=0),
            ),
            pb.ReplayEvaluationResult,
        )
    )


def program(
    native: pb.ReplayMeasurementPlan,
) -> Callable[[ReplayFlow], Coroutine[object, object, object]]:
    async def root(ctx: ReplayFlow) -> object:
        submitted = await step(ctx, Oneof("submit", pb.ReplaySubmitMeasurement(plan=native)))
        assert submitted.resource
        current = submitted
        while current.job is None or not current.job.terminal:
            sample = "initial" if current.job is None else str(current.job.observation_sequence)
            current = await step(
                ctx,
                Oneof("observe", pb.ReplayMeasurementJob(resource=submitted.resource)),
                sample=sample,
            )
        assert current.job.status is core_vocabulary.ObservationStatus.SUCCEEDED
        evidence = await step(
            ctx, Oneof("collect", pb.ReplayMeasurementJob(resource=submitted.resource))
        )
        assert evidence.released
        assert evidence.job is not None
        assert evidence.job.evidence
        cancelled = await step(
            ctx, Oneof("cancel", pb.ReplayCancelMeasurement(resource=submitted.resource))
        )
        assert cancelled.released
        closed = await step(ctx, Oneof("close", pb.ReplayCloseMeasurements()))
        assert closed.released
        assert submitted.resource in closed.children
        return evidence.job.evidence

    return root


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
@pytest.mark.parametrize("effect", ["submit", "observe", "collect", "cancel", "close"])
async def test_evaluation_recovery_preserves_job_evidence_and_release(
    tmp_path: Path, sim: Sim, build: _Build, effect: str, boundary: str
) -> None:
    world = await build(tmp_path / "owner", _Script.SUCCEED, None)
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    store = FakeStateStore()
    native = replay_measurement_plan(plan(candidate=world.snapshot))
    root = program(native)

    def executor() -> EvaluationReplayExecutor:
        return EvaluationReplayExecutor(
            world.executor,
            ReceiptStore(namespace, SimThreads()),
            ReplayOperationHost("run", store, sim.clock),
            within_seconds=100,
        )

    first_executor = executor()
    runtime = ReplayRuntime(store, root, replay.ReplayAllowance())
    try:
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        while True:
            (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
            selected = span.name == "evaluation." + effect
            if selected and boundary == "prepared":
                break
            result = await runtime.dispatch(span.id, first_executor, now=sim.clock.now())
            if selected and boundary == "executed":
                break
            runtime.observe(span.id, result, now=sim.clock.now(), at=Timestamp())
            if selected and boundary == "observed":
                break
            runtime.accept(span.id, now=sim.clock.now(), at=Timestamp())
            if selected:
                break
        runtime.close(now=sim.clock.now())
        resumed = host(store, root, executor(), sim)
        result = await drive_replay(
            resumed, ReplayLaunch(EpochStarted(run_id="run"), "resume", "host-a", 100)
        )
        assert isinstance(result, ReplayCompleted)
        assert result.value
        assert resumed.runtime.accounting.paid_operations == 1
        assert resumed.runtime.accounting.unreported_cost_operations == 1
        again = host(store, root, executor(), sim)
        assert (
            await drive_replay(
                again,
                ReplayLaunch(EpochStarted(run_id="run"), "again", "host-a", 100),
            )
            == result
        )
        assert again.runtime.accounting.paid_operations == 1
        assert again.runtime.accounting.unreported_cost_operations == 1
    finally:
        await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_evaluation_recovery_sweeps_owner_receipts(
    tmp_path_factory: pytest.TempPathFactory, sim: Sim, build: _Build
) -> None:
    async def run_case(root: Path, crash_at: int | None) -> int:
        world = await build(root / "owner", _Script.SUCCEED, None)
        namespace = Project.open(root).state.state_store_namespace("run")
        faulting = FaultingNamespace(namespace, crash_at)
        store = FakeStateStore()
        root_flow = program(replay_measurement_plan(plan(candidate=world.snapshot)))

        def executor(*, restarting: bool = False) -> EvaluationReplayExecutor:
            receipts = ReceiptStore(
                namespace if restarting else cast("StateNamespace", faulting), SimThreads()
            )
            return EvaluationReplayExecutor(
                world.executor,
                receipts,
                ReplayOperationHost("run", store, sim.clock),
                within_seconds=100,
            )

        try:
            first = host(store, root_flow, executor(), sim)
            launch = ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100)
            if crash_at is None:
                result = await drive_replay(first, launch)
            else:
                with pytest.raises(ProcessKilledError):
                    await drive_replay(first, launch)
                unresolved = ()
                try:
                    result = await drive_replay(
                        host(store, root_flow, executor(restarting=True), sim),
                        ReplayLaunch(EpochStarted(run_id="run"), "resume", "host-a", 100),
                    )
                except ReplayRecoveryRequiredError as error:
                    # No task exists in a local client when the crash precedes
                    # dispatch. Its lack of durable absence proof must remain
                    # unknown, rather than authorizing another submission.
                    unresolved = error.spans
                if unresolved:
                    assert not world.workspaces.candidates
                    for span in unresolved:
                        assert span.name == "evaluation.submit"
                        knowledge = await world.executor.recover(
                            handle_for(RequestId(root=span.id))
                        )
                        assert knowledge.phase is PollPhase.UNKNOWN
                    return faulting.writes
            assert isinstance(result, ReplayCompleted)
            assert result.value
            assert len(world.workspaces.candidates) == 1
        finally:
            await world.executor.close()
        assert all(candidate.discarded for candidate in world.workspaces.candidates)
        return faulting.writes

    writes = await run_case(tmp_path_factory.mktemp("baseline"), None)
    for boundary in swept(2 * writes, sample=3):
        await run_case(tmp_path_factory.mktemp(f"crash-{boundary}"), boundary)


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("mutation", ["bound", "request_type", "reply_type", "unknown_field"])
async def test_evaluation_rejects_invalid_native_dispatch_before_submission(
    tmp_path: Path, sim: Sim, build: _Build, mutation: str
) -> None:
    world = await build(tmp_path / "owner", _Script.SUCCEED, None)
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    store = FakeStateStore()
    root = program(replay_measurement_plan(plan(candidate=world.snapshot)))
    runtime = ReplayRuntime(store, root, replay.ReplayAllowance())
    try:
        runtime.start(
            ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100),
            now=sim.clock.now(),
            at=Timestamp(),
        )
        (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
        runtime.close(now=sim.clock.now())
        assert span.replay is not None
        assert span.replay.policy is not None
        assert span.replay.policy.request is not None
        policy = span.replay.policy
        assert policy.request is not None
        match mutation:
            case "bound":
                native = pb.ReplayEvaluationRequest.from_binary(policy.request.value)
                native.within_seconds = 1
                policy.request.value = native.to_binary()
            case "request_type":
                policy.request.type_url = "invalid.Request"
            case "reply_type":
                policy.reply_type = "invalid.Reply"
            case "unknown_field":
                policy.request.value += b"\xa0\x06\x01"
        fence = store.acquire("host-a", sim.clock.now(), 100)
        assert fence is not None
        executor = EvaluationReplayExecutor(
            world.executor,
            ReceiptStore(namespace, SimThreads()),
            ReplayOperationHost("run", store, sim.clock),
            within_seconds=100,
        )
        with pytest.raises(ReplayOperationError, match=r"bound|type differs|unknown fields"):
            await executor.execute(span, fence)
        assert not world.workspaces.candidates
        assert store.release(fence, sim.clock.now())
    finally:
        await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_removing_a_measurement_discloses_paid_work_with_unreported_cost(
    tmp_path: Path, sim: Sim, build: _Build
) -> None:
    world = await build(tmp_path / "owner", _Script.SUCCEED, None)
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    store = FakeStateStore()
    executor = EvaluationReplayExecutor(
        world.executor,
        ReceiptStore(namespace, SimThreads()),
        ReplayOperationHost("run", store, sim.clock),
        within_seconds=100,
    )

    async def edited(_ctx: ReplayFlow) -> object:
        return "no measurement"

    try:
        original = host(
            store, program(replay_measurement_plan(plan(candidate=world.snapshot))), executor, sim
        )
        result = await drive_replay(
            original, ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100)
        )
        assert isinstance(result, ReplayCompleted)
        changed = host(store, edited, executor, sim)
        assert (
            await drive_replay(
                changed, ReplayLaunch(EpochStarted(run_id="run"), "edited", "host-a", 100)
            )
            == ReplayParked()
        )
        (notice_bytes,) = changed.runtime.archive.acknowledgements.notices
        notice = replay.ReplayNotice.from_binary(notice_bytes)
        (unused,) = notice.unused
        assert unused.key is not None
        assert unused.key.operation == "evaluation.submit"
        assert unused.paid
        assert not unused.has_field("cost_usd")
        assert changed.runtime.accounting.paid_operations == 1
        assert changed.runtime.accounting.unreported_cost_operations == 1
        assert changed.runtime.accounting.reported_cost_usd == 0
        assert len(world.workspaces.candidates) == 1
    finally:
        await world.executor.close()
