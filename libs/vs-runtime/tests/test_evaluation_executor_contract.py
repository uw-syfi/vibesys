"""Both semantic evaluation executors honor one lifecycle contract.

The in-process ``PollingEvaluationExecutor`` (over a Fake ``Evaluation``) and
the ``SemanticSlurmEvaluationExecutor`` (over the Fake Slurm cluster) are
interchangeable behind ``core_bindings(evaluation=...)``, so each must submit,
reach a terminal state, render a failed stage's text, and cancel the same way.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import pytest
from tests.support.runtime_evaluation import ScenarioCluster, build_stack

from vs_evaluation.api import (
    ContentDigest,
    EvaluationExecutor,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ExecutorObservation,
    SemanticEvaluationStage,
    StageState,
    TrustedEvidence,
)
from vs_runtime.api import (
    AccuracyEvaluation,
    PollingEvaluationExecutor,
    render_stage_failure,
)
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_slurm.api import SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})
_HANDLE = "handle-1"


class _Executor(EvaluationExecutor, Protocol):
    async def close(self) -> None: ...


@dataclass
class _World:
    """One executor over Fakes, scripted to pass, fail accuracy, or hold the first stage."""

    executor: _Executor
    snapshot: str
    stage_running: Callable[[], Awaitable[None]]


class _Script(StrEnum):
    SUCCEED = "succeed"
    FAIL_ACCURACY = "fail-accuracy"
    HOLD_FIRST_STAGE = "hold-first-stage"


type _Build = Callable[[Path, _Script], Awaitable[_World]]


async def _local(root: Path, script: _Script) -> _World:
    del root
    evaluation = FakeEvaluation()
    if script is _Script.FAIL_ACCURACY:
        evaluation.accuracy_results.append(
            AccuracyEvaluation(executed=True, feedback="accuracy mismatch")
        )
    else:
        evaluation.default_accuracy = AccuracyEvaluation(executed=True)
    gate = evaluation.gate("accuracy", 0) if script is _Script.HOLD_FIRST_STAGE else None
    workspaces = FakeWorkspaces(FakeWorkspace(), supports_parallel_candidates=True)
    snapshot = await workspaces.root.snapshot("candidate")

    async def running() -> None:
        if gate is not None:
            await gate.entered.wait()

    return _World(PollingEvaluationExecutor(evaluation, workspaces), snapshot, running)


async def _slurm(root: Path, script: _Script) -> _World:
    cluster = ScenarioCluster()
    cluster.accuracy_exit = 1 if script is _Script.FAIL_ACCURACY else 0
    if script is _Script.HOLD_FIRST_STAGE:
        cluster.states = (SlurmJobStatus.PENDING,)
    stack = await build_stack(root, cluster)

    async def running() -> None:
        return None

    return _World(stack.executor, stack.snapshot, running)


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
    while True:
        observed = await executor.inspect(_HANDLE)
        if observed is not None and observed.state in _TERMINAL:
            return observed
        await asyncio.sleep(0)


@pytest.mark.parametrize("build", BUILDERS)
async def test_a_submitted_request_is_observable_and_succeeds_with_evidence_per_stage(
    build: _Build, tmp_path: Path
) -> None:
    world = await build(tmp_path, _Script.SUCCEED)
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
    world = await build(tmp_path, _Script.SUCCEED)
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
    world = await build(tmp_path, _Script.FAIL_ACCURACY)

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
    world = await build(tmp_path, _Script.HOLD_FIRST_STAGE)
    await world.executor.submit(_request(world.snapshot), handle_id=_HANDLE)
    await world.stage_running()

    await world.executor.cancel(_HANDLE)
    observed = await _terminal(world.executor)

    assert observed.state is EvaluationState.CANCELED
    await world.executor.close()
