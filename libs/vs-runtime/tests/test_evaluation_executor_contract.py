"""Both semantic evaluation executors honor one lifecycle contract.

The in-process ``PollingEvaluationExecutor`` (over a Fake ``Evaluation``) and
the ``SemanticSlurmEvaluationExecutor`` (over the Fake Slurm cluster) are
interchangeable behind ``core_bindings(measurement=MeasurementServices(...))``, so each must submit,
reach a terminal state, render a failed stage's text, and cancel the same way.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ExecutorObservation,
    ExecutorPoll,
    PollPhase,
    SemanticEvaluationStage,
    StageState,
    TrustedEvidence,
)
from vs_evaluation.api import (
    PollingEvaluationExecutor as PollingExecutorPort,
)
from vs_evaluation.api.testing import wait_until_executor_started
from vs_runtime.api import (
    AccuracyEvaluation,
    BenchmarkFailureKind,
    PollingEvaluationExecutor,
    render_stage_failure,
)
from vs_runtime.api.core import StageOutputOpener, StageRun
from vs_runtime.api.slurm_evaluation_stack import BENCHMARK_OUTPUT, ScenarioCluster, build_stack
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api import Capture, OutputStream
from vs_sim.api.testing import ManualClock, yield_until
from vs_slurm.api import SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

pytestmark = pytest.mark.asyncio

_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})
_HANDLE = "handle-1"


class _Executor(PollingExecutorPort, Protocol):
    """The protocol the request executor uses, so a missing method fails type checking."""

    async def close(self) -> None: ...


@dataclass
class _World:
    """One executor over Fakes, scripted to pass, fail accuracy, or hold the first stage."""

    executor: _Executor
    snapshot: str
    stage_running: Callable[[], Awaitable[None]]
    submission_visible: Callable[[str], Awaitable[None]]
    printed: dict[str, bytes] = field(default_factory=dict)
    """What each stage's command printed to stdout, as the executor's Fake scripts it."""


class _Script(StrEnum):
    SUCCEED = "succeed"
    FAIL_ACCURACY = "fail-accuracy"
    HOLD_FIRST_STAGE = "hold-first-stage"


type _Build = Callable[[Path, _Script, StageOutputOpener | None], Awaitable[_World]]
_LOCAL_PRINTED = {"accuracy": b"checking 1/3\nchecking 2/3\n", "benchmark": b"warmup\nrps=41\n"}


async def _local(root: Path, script: _Script, stage_output: StageOutputOpener | None) -> _World:
    del root
    evaluation = FakeEvaluation()
    evaluation.printed.update({"accuracy": (_LOCAL_PRINTED["accuracy"], b"")})
    evaluation.printed.update({"benchmark": (_LOCAL_PRINTED["benchmark"], b"")})
    if script is _Script.FAIL_ACCURACY:
        evaluation.accuracy_results.append(
            AccuracyEvaluation(
                executed=True,
                feedback="accuracy mismatch",
                failure_kind=BenchmarkFailureKind.WORKLOAD,
            )
        )
    else:
        evaluation.default_accuracy = AccuracyEvaluation(executed=True)
    gate = evaluation.gate("accuracy", 0) if script is _Script.HOLD_FIRST_STAGE else None
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
    )
    snapshot = await workspaces.root.snapshot("candidate")

    async def running() -> None:
        if gate is not None:
            await gate.entered.wait()

    async def visible(_handle_id: str) -> None:
        return None

    return _World(
        PollingEvaluationExecutor(
            evaluation, workspaces, clock=ManualClock(), stage_output=stage_output
        ),
        snapshot,
        running,
        visible,
        dict(_LOCAL_PRINTED),
    )


async def _slurm(root: Path, script: _Script, stage_output: StageOutputOpener | None) -> _World:
    cluster = ScenarioCluster()
    cluster.accuracy_exit = 1 if script is _Script.FAIL_ACCURACY else 0
    if script is _Script.HOLD_FIRST_STAGE:
        cluster.states = (SlurmJobStatus.PENDING,)
    stack = await build_stack(root, cluster, stage_output=stage_output)

    async def running() -> None:
        return None

    async def visible(handle_id: str) -> None:
        # submit() returns before the provider has the job: the cluster call runs
        # in a background task, and until it lands a poll honestly reports Unknown.
        await wait_until_executor_started(cluster.accepted, stack.executor, handle_id)

    printed = {"accuracy": b"ok", "benchmark": BENCHMARK_OUTPUT.encode()}
    return _World(stack.executor, stack.snapshot, running, visible, printed)


BUILDERS = [pytest.param(_local, id="polling"), pytest.param(_slurm, id="semantic-slurm")]


def _request(snapshot: str) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="contract",
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


async def _terminal(executor: _Executor) -> ExecutorObservation:
    observed = await yield_until(
        lambda: executor.inspect(_HANDLE),
        lambda seen: seen is not None and seen.state in _TERMINAL,
        "a terminal observation",
    )
    assert observed is not None
    return observed


@pytest.mark.parametrize("build", BUILDERS)
async def test_a_submitted_request_is_observable_and_succeeds_with_evidence_per_stage(
    build: _Build, tmp_path: Path
) -> None:
    world = await build(tmp_path, _Script.SUCCEED, None)
    assert await world.executor.inspect(_HANDLE) is None

    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)
    observed = await _terminal(world.executor)

    assert observed.state is EvaluationState.SUCCEEDED
    assert observed.failure is None
    assert [step.name for step in observed.stage_results] == ["accuracy", "benchmark"]
    assert all(step.state is StageState.SUCCEEDED for step in observed.stage_results)
    for step in observed.stage_results:
        evidence = TrustedEvidence.model_validate(step.result)
        assert evidence.evaluation_id == _HANDLE
        assert evidence.outcome is EvidenceOutcome.PASSED
    await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_resubmitting_the_same_handle_is_idempotent(build: _Build, tmp_path: Path) -> None:
    world = await build(tmp_path, _Script.SUCCEED, None)
    request = _request(world.snapshot)

    await world.executor.submit(request, handle_id=_HANDLE)
    await world.executor.submit(request, handle_id=_HANDLE)
    observed = await _terminal(world.executor)

    assert observed.state is EvaluationState.SUCCEEDED
    await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_a_failed_accuracy_stage_fails_the_evaluation_with_its_rendered_text(
    build: _Build, tmp_path: Path
) -> None:
    world = await build(tmp_path, _Script.FAIL_ACCURACY, None)

    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)
    observed = await _terminal(world.executor)

    assert observed.state is EvaluationState.FAILED
    accuracy, benchmark = observed.stage_results
    assert benchmark.state is StageState.SKIPPED
    evidence = TrustedEvidence.model_validate(accuracy.result)
    assert evidence.outcome is EvidenceOutcome.FAILED
    assert evidence.semantic_summary
    assert observed.failure == render_stage_failure(
        ((evidence.semantic_summary, EvidenceKind.ACCURACY),), None
    )
    await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_cancel_stops_an_in_flight_evaluation_and_reports_canceled(
    build: _Build, tmp_path: Path
) -> None:
    world = await build(tmp_path, _Script.HOLD_FIRST_STAGE, None)
    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)
    await world.stage_running()

    await world.executor.cancel(_HANDLE)
    observed = await _terminal(world.executor)

    assert observed.state is EvaluationState.CANCELED
    await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_poll_reports_each_phase_without_submitting_or_cancelling(
    build: _Build, tmp_path: Path
) -> None:
    world = await build(tmp_path, _Script.SUCCEED, None)
    assert (await world.executor.poll(_HANDLE)).phase is PollPhase.UNSUBMITTED
    assert await world.executor.inspect(_HANDLE) is None  # polling submitted nothing

    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)
    await world.submission_visible(_HANDLE)

    def ended(polled: ExecutorPoll) -> bool:
        assert polled.phase in (PollPhase.QUEUED, PollPhase.RUNNING, PollPhase.ENDED)
        return polled.phase is PollPhase.ENDED

    polled = await yield_until(lambda: world.executor.poll(_HANDLE), ended, "the poll's end")

    assert polled.terminal is not None
    assert polled.terminal.state is EvaluationState.SUCCEEDED
    await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_cancelling_a_finished_evaluation_keeps_its_terminal_result(
    build: _Build, tmp_path: Path
) -> None:
    """Cancel is a request: an evaluation that already ended keeps its result.

    Closing a scope cancels every job it registered, finished ones included, and
    then polls each to prove release. A cancel that rewrites a SUCCEEDED
    evaluation as CANCELED discards evidence the run already paid for.
    """
    world = await build(tmp_path, _Script.SUCCEED, None)
    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)
    finished = await _terminal(world.executor)
    assert finished.state is EvaluationState.SUCCEEDED

    await world.executor.cancel(_HANDLE)

    inspected = await world.executor.inspect(_HANDLE)
    assert inspected is not None
    assert inspected.state is EvaluationState.SUCCEEDED, inspected
    polled = await world.executor.poll(_HANDLE)
    assert polled.phase is PollPhase.ENDED
    assert polled.terminal is not None
    assert polled.terminal.state is EvaluationState.SUCCEEDED
    assert polled.terminal.stage_results == finished.stage_results
    await world.executor.close()


@pytest.mark.parametrize("build", BUILDERS)
async def test_close_ends_every_submitted_evaluation_in_a_terminal_state(
    build: _Build, tmp_path: Path
) -> None:
    """Closing the executor right after a submit leaves no evaluation non-terminal."""
    world = await build(tmp_path, _Script.HOLD_FIRST_STAGE, None)
    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)

    await world.executor.close()

    observed = await world.executor.inspect_only(_HANDLE)
    assert observed is not None
    assert observed.state in _TERMINAL, observed


@dataclass
class _Streams:
    """An opener that keeps each stage's stdout and records open and close, in order."""

    opened: list[StageRun] = field(default_factory=list)
    closed: list[StageRun] = field(default_factory=list)
    output: dict[StageRun, Capture] = field(default_factory=dict)

    @contextmanager
    def open(self, run: StageRun) -> Iterator[Capture]:
        self.opened.append(run)
        sink = self.output[run] = Capture(1 << 16)
        try:
            yield sink
        finally:
            self.closed.append(run)


@pytest.mark.parametrize("build", BUILDERS)
async def test_each_stage_output_reaches_one_stream_named_by_its_run(
    build: _Build, tmp_path: Path
) -> None:
    """Local or on the cluster, a stage's whole stdout lands in one stream, closed at its end."""
    streams = _Streams()
    world = await build(tmp_path, _Script.SUCCEED, streams.open)

    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)
    observed = await _terminal(world.executor)
    await world.executor.close()

    runs = [StageRun(_HANDLE, "accuracy"), StageRun(_HANDLE, "benchmark")]
    assert observed.state is EvaluationState.SUCCEEDED
    assert streams.opened == runs
    assert sorted(streams.closed, key=runs.index) == runs
    assert {
        run.stage_id: streams.output[run].kept(OutputStream.STDOUT) for run in runs
    } == world.printed
