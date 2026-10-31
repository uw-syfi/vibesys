"""The polling executor writes each stage's command output to a stream of that stage.

A stage's output reaches the stream opened for it while the stage runs, before it
settles, and the stream is closed when the stage ends, however it ends.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

from vs_evaluation.api import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    ExecutorObservation,
    SemanticEvaluationStage,
    TrustedEvidence,
)
from vs_redaction.api import redact_diagnostic_tail
from vs_runtime.api import (
    AccuracyEvaluation,
    BenchmarkEvaluation,
    BenchmarkFailureKind,
    PollingEvaluationExecutor,
)
from vs_runtime.api.core import StageRun
from vs_runtime.api.infrastructure import create_run_control_channel
from vs_runtime.api.testing import (
    FakeEvaluation,
    FakeRunControlEventSink,
    FakeWorkspace,
    FakeWorkspaces,
)
from vs_sim.api import Capture
from vs_sim.api.testing import Gate, ManualClock, SimThreads, arrival, yield_until

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_runtime.api import CandidateWorkspace

pytestmark = pytest.mark.asyncio

_HANDLE = "handle-1"
_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})
_ACCURACY = StageRun(_HANDLE, "accuracy")
_BENCHMARK = StageRun(_HANDLE, "benchmark")
_PRINTED: dict[Literal["build", "accuracy", "benchmark"], tuple[bytes, bytes]] = {
    "accuracy": (b"checking 1/3\nchecking 2/3\n", b"warning: slow\n"),
    "benchmark": (b"warmup\nrps=41\n", b""),
}


class _Streams:
    """An opener that keeps every byte of each stage's stream and records open and close."""

    def __init__(self) -> None:
        self.opened: list[StageRun] = []
        self.closed: list[StageRun] = []
        self.output: dict[StageRun, Capture] = {}

    @contextmanager
    def open(self, run: StageRun) -> Iterator[Capture]:
        self.opened.append(run)
        sink = self.output[run] = Capture(1 << 16)
        try:
            yield sink
        finally:
            self.closed.append(run)

    def written(self, run: StageRun) -> tuple[bytes, bytes]:
        sink = self.output[run]
        return sink.stdout, sink.stderr


def _request(snapshot: str) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="stage-output",
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


async def _start(
    evaluation: FakeEvaluation,
) -> tuple[PollingEvaluationExecutor, _Streams]:
    evaluation.printed.update(_PRINTED)
    streams = _Streams()
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
    )
    executor = PollingEvaluationExecutor(
        evaluation, workspaces, clock=ManualClock(), stage_output=streams.open
    )
    snapshot = await workspaces.root.snapshot("candidate")
    await executor.submit(_request(snapshot), handle_id=_HANDLE)
    return executor, streams


async def _terminal(executor: PollingEvaluationExecutor) -> ExecutorObservation:
    observed = await yield_until(
        lambda: executor.inspect(_HANDLE),
        lambda seen: seen is not None and seen.state in _TERMINAL,
        "a terminal observation",
    )
    assert observed is not None
    return observed


async def test_a_stage_writes_its_output_to_its_own_stream_before_it_settles() -> None:
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    gate = evaluation.gate("accuracy", 0)
    executor, streams = await _start(evaluation)

    ended = asyncio.create_task(_terminal(executor))
    await arrival(gate.entered.wait(), ended)
    live = (list(streams.opened), list(streams.closed), streams.written(_ACCURACY))
    gate.release()
    observed = await ended

    assert live == ([_ACCURACY], [], _PRINTED["accuracy"])
    assert observed.state is EvaluationState.SUCCEEDED
    assert streams.opened == streams.closed == [_ACCURACY, _BENCHMARK]
    assert streams.written(_BENCHMARK) == _PRINTED["benchmark"]
    await executor.close()


@pytest.mark.parametrize("kind", [EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK])
@pytest.mark.parametrize("split_at", range(1, len("API_TOKEN")))
async def test_local_stage_keeps_raw_evidence_and_separate_operator_failure(
    kind: EvidenceKind,
    split_at: int,
) -> None:
    credential = "API_TOKEN=local-summary-secret"
    suffix = "z" * (MAX_EVIDENCE_SUMMARY_CHARS + split_at - len(credential) - 1)
    feedback = "prefix:" + credential + " " + suffix
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    if kind is EvidenceKind.ACCURACY:
        evaluation.script_accuracy(
            AccuracyEvaluation(
                executed=True,
                feedback=feedback,
                operator_feedback=redact_diagnostic_tail(
                    feedback, max_chars=MAX_EVIDENCE_SUMMARY_CHARS
                ),
                failure_kind=BenchmarkFailureKind.WORKLOAD,
            )
        )
    else:
        evaluation.script_benchmark(
            BenchmarkEvaluation(
                executed=True,
                feedback=feedback,
                operator_feedback=redact_diagnostic_tail(
                    feedback, max_chars=MAX_EVIDENCE_SUMMARY_CHARS
                ),
                failure_kind=BenchmarkFailureKind.WORKLOAD,
            )
        )
    executor, _streams = await _start(evaluation)

    observed = await _terminal(executor)
    stage = next(stage for stage in observed.stage_results if stage.name == kind.value)
    evidence = TrustedEvidence.model_validate(stage.result)

    assert evidence.semantic_summary is not None
    assert "local-summary-secret" in evidence.semantic_summary
    assert len(evidence.semantic_summary) <= MAX_EVIDENCE_SUMMARY_CHARS
    assert stage.operator_summary is not None
    assert "local-summary-secret" not in stage.operator_summary
    await executor.close()


class _Ending(StrEnum):
    FAILS = "fails"
    RAISES = "raises"
    CANCELLED = "cancelled"


@pytest.mark.parametrize("ending", list(_Ending))
async def test_a_stage_that_ends_early_closes_its_stream_with_what_it_wrote(
    ending: _Ending,
) -> None:
    evaluation = FakeEvaluation()
    if ending is _Ending.FAILS:
        evaluation.script_accuracy(
            AccuracyEvaluation(
                executed=True,
                feedback="mismatch",
                operator_feedback="mismatch",
                failure_kind=BenchmarkFailureKind.WORKLOAD,
            )
        )
    elif ending is _Ending.RAISES:
        evaluation.script_accuracy(RuntimeError("evaluator crashed"))
    gate = evaluation.gate("accuracy", 0) if ending is _Ending.CANCELLED else None
    executor, streams = await _start(evaluation)

    ended = asyncio.create_task(_terminal(executor))
    if gate is not None:
        await arrival(gate.entered.wait(), ended)
        await executor.cancel(_HANDLE)
    observed = await ended

    assert observed.state is not EvaluationState.SUCCEEDED
    assert streams.opened == streams.closed == [_ACCURACY]
    assert streams.written(_ACCURACY) == _PRINTED["accuracy"]
    await executor.close()


class _PreparingWorkspaces(FakeWorkspaces):
    """Hold the actual candidate preparation, retaining Fake workspace behavior."""

    def __init__(self) -> None:
        super().__init__(FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True)
        self.preparing = Gate()
        self.release = Gate()

    async def create_measured_candidate(
        self, revision: str, *, step_tag: str | None = None
    ) -> CandidateWorkspace:
        self.preparing.open()
        await self.release.wait()
        return await super().create_measured_candidate(revision, step_tag=step_tag)


async def test_pause_drain_cancels_a_job_still_preparing_its_workspace() -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    workspaces = _PreparingWorkspaces()
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    executor = PollingEvaluationExecutor(
        evaluation, workspaces, clock=ManualClock(), control=control
    )
    snapshot = await workspaces.root.snapshot("candidate")
    await executor.submit(_request(snapshot), handle_id=_HANDLE)
    ended = asyncio.create_task(_terminal(executor))
    await workspaces.preparing.wait(ended)
    control.land_park()
    workspaces.release.open()
    assert (await ended).state is EvaluationState.CANCELED
    assert evaluation.accuracy_calls == []
    assert evaluation.benchmark_calls == []
    await executor.close()


async def test_pause_drain_lets_every_stage_of_an_executing_job_finish() -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
    )
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    gate = evaluation.gate("accuracy", 0)
    executor = PollingEvaluationExecutor(
        evaluation, workspaces, clock=ManualClock(), control=control
    )
    snapshot = await workspaces.root.snapshot("candidate")
    await executor.submit(_request(snapshot), handle_id=_HANDLE)
    ended = asyncio.create_task(_terminal(executor))
    await arrival(gate.entered.wait(), ended)
    control.land_park()
    gate.release()
    assert (await ended).state is EvaluationState.SUCCEEDED
    assert len(evaluation.accuracy_calls) == len(evaluation.benchmark_calls) == 1
    await executor.close()
