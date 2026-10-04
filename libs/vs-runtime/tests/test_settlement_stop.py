"""Stops cancel suspended observation while preserving owned evaluation jobs."""

from __future__ import annotations

import asyncio

import pytest

from vs_evaluation.api import (
    ContentDigest,
    EvaluationPending,
    EvaluationRequest,
    EvaluationStep,
    EvidenceFingerprints,
    OwnedEvaluationDependencies,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements
from vs_runtime.api.infrastructure import (
    RunStopped,
    create_run_control_channel,
    stop_gated_evaluation,
)
from vs_runtime.api.testing import FakeEvaluation, FakeRunControlEventSink


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["observe", "wait_any"])
async def test_stop_cancels_only_the_host_observer(operation: str) -> None:
    fake = FakeEvaluationSettlements()
    digest = ContentDigest.sha256(b"candidate")
    handle = await fake.submit(
        EvaluationRequest(
            key="work",
            owner_scope="scope",
            stages=(EvaluationStep(name="benchmark", payload={}),),
        ),
        EvidenceFingerprints(
            candidate=digest,
            evaluator=digest,
            workload=digest,
            environment=digest,
        ),
    )
    dependencies = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    channel = create_run_control_channel(FakeRunControlEventSink())
    evaluation = stop_gated_evaluation(FakeEvaluation(settlement_observations=fake), channel)
    if operation == "observe":
        release = fake.backend.hold_record_reads()
        entered = fake.backend.record_read_started
        call = evaluation.settlements().observe(dependencies)
    else:
        release = None
        entered = fake.executor.wait_started
        call = evaluation.settlements().wait_any(dependencies)
    task = asyncio.create_task(call)
    await entered.wait()
    channel.request_stop()
    with pytest.raises(RunStopped):
        await task
    if release is not None:
        release.set()
    assert fake.executor.cancellations == []
    assert isinstance((await fake.observe(dependencies))[0].result, EvaluationPending)
