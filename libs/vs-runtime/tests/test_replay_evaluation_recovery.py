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
from vs_project.api import FakeStateStore, Project
from vs_proto.api import EpochStarted, replay
from vs_proto.api import replay_evaluation as pb
from vs_runtime.api.core import ReceiptStore, handle_for
from vs_runtime.api.replay import (
    EvaluationReplayExecutor,
    ReplayLaunch,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayResult,
    ReplayRuntime,
    ReplayUnknown,
    replay_measurement_plan,
)
from vs_runtime.api.slurm_evaluation_stack import plan
from vs_sim.api.testing import SimThreads, yield_until

from .test_evaluation_executor_contract import _local, _Script
from .test_replay_evaluation import SCOPE, program

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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


async def local_recovery_case(tmp_path: Path, sim: Sim, implementation: Factory) -> None:
    old = await _local(tmp_path / "old", _Script.HOLD_FIRST_STAGE, None)
    replacement = await _local(tmp_path / "new", _Script.SUCCEED, None)
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    store = FakeStateStore()
    timing = ReplayOperationHost("run", store, sim.clock)
    crashing = implementation(
        old.executor, CrashOnReplace(namespace, threads=SimThreads()), timing, within_seconds=100
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
            replacement.executor, ReceiptStore(namespace, SimThreads()), timing, within_seconds=100
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
async def test_empty_new_local_client_neither_repeats_nor_claims_to_cancel_old_work(
    tmp_path: Path, sim: Sim
) -> None:
    await local_recovery_case(tmp_path, sim, EvaluationReplayExecutor)
