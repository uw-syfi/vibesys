"""A submission racing the semantic Slurm executor's close leaves no job on the cluster.

Regression: ``close`` closed the candidate executors it had, then forgot them.
A submission still on its way (validating its revision, or waiting for the
executor's lock) then made a new candidate executor that nothing closed, and
its job stayed queued after the run exited.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    ExecutorSubmissionError,
    SemanticEvaluationStage,
)
from vs_runtime.api.infrastructure import create_run_control_channel
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api import yield_now
from vs_sim.api.testing import SimThreads, VirtualClock, examples, run_virtual, yield_until
from vs_slurm.api import ClusterObservation, SlurmJobStatus

if TYPE_CHECKING:
    from pathlib import Path

# Loop turns, then virtual seconds, after the steps for work left behind to reach the scheduler.
_SETTLE_TURNS = 20
_SETTLE_S = 10.0


def _request(snapshot: str) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="close-race",
        stages=tuple(
            EvaluationStep(
                name=kind.value,
                payload=SemanticEvaluationStage(
                    snapshot=snapshot, kind=kind, fingerprints=fingerprints
                ).model_dump(mode="json"),
            )
            for kind in (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
        ),
    )


async def _queued_after_close(
    clock: VirtualClock, root: Path, turns_before_close: int
) -> list[str]:
    """Submit, close ``turns_before_close`` loop turns later, and read what is still queued."""
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)  # only a cancel ends a job
    stack = await build_stack(root, cluster)

    async def submit() -> None:
        # Refused while closing: nothing was sent.
        with contextlib.suppress(ExecutorSubmissionError):
            await stack.executor.submit(_request(stack.snapshot), handle_id="race-0")

    submitting = asyncio.ensure_future(submit())
    for _ in range(turns_before_close):
        await yield_now()
    await stack.executor.close()
    await submitting
    for _ in range(_SETTLE_TURNS):
        await yield_now()
    await clock.sleep(_SETTLE_S)
    queued = []
    for operation in cluster.submissions:
        observed = cluster.inspect(operation)
        if isinstance(observed, ClusterObservation) and not observed.status.terminal:
            queued.append(operation)
    return queued


@pytest.mark.regression
@settings(max_examples=examples(pr=20, nightly=200))
@given(
    turns_before_close=st.integers(min_value=0, max_value=12),
    schedule_seed=st.integers(min_value=0, max_value=2**16),
)
# The close runs while the submission validates its revision.
@example(turns_before_close=0, schedule_seed=0)
def test_no_job_is_left_queued_by_a_submission_that_raced_the_close(
    tmp_path_factory: pytest.TempPathFactory, turns_before_close: int, schedule_seed: int
) -> None:
    root = tmp_path_factory.mktemp("stack")

    clock = VirtualClock()
    queued = run_virtual(
        clock,
        _queued_after_close(clock, root, turns_before_close),
        schedule_seed=schedule_seed,
    )

    assert queued == []


async def test_pause_drain_refuses_prepared_cluster_execution(tmp_path: Path) -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    cluster = ScenarioCluster()
    stack = await build_stack(tmp_path, cluster, control=control)
    control.land_park()
    await stack.executor.submit(_request(stack.snapshot), handle_id="pause-drain")
    observed = await yield_until(
        lambda: stack.executor.inspect("pause-drain"),
        lambda seen: seen is not None and seen.state is EvaluationState.FAILED,
        "the pause drain refusal",
    )
    assert observed is not None
    assert "pause drain" in (observed.failure or "")
    assert cluster.submissions == []
    await stack.executor.close()
