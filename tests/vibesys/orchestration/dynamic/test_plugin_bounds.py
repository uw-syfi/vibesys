"""Repeated submissions stop a charged attempt through the public dynamic plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import dynamic_options, implementation
from tests.vibesys.orchestration.dynamic.test_continuation_bounds import (
    _TRACEBACK,
    _submit_failures,
)
from tests.vibesys.orchestration.dynamic.test_plugin_suspension import ResumeScript, _open

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.models import DynamicState, WorkstreamPhase
from vibesys.run.evaluation_backend import agent_evaluation
from vs_evaluation.api import (
    ContentDigest,
    EvaluationState,
    EvaluationStepResult,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    FailureKind,
    PartialMeasurement,
    StageState,
    TrustedEvidence,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.vibesys.orchestration.dynamic.test_plugin_suspension import _Scenario

    from vs_agent.api import AgentTurnRequest


@pytest.mark.parametrize("kind", [FailureKind.TRACEBACK, FailureKind.MEASUREMENT])
@pytest.mark.parametrize("omitted", [False, True])
@pytest.mark.parametrize("return_final", [False, True])
@pytest.mark.asyncio
async def test_public_plugin_bounds_incremental_evaluations(
    tmp_path: Path, kind: FailureKind, *, omitted: bool, return_final: bool
) -> None:
    answer: dict[str, object] = {}
    loop = asyncio.get_running_loop()
    submissions = 1

    def resumed(request: AgentTurnRequest) -> None:
        nonlocal submissions
        if request.invocation_id is None:
            return
        if submissions >= 5:
            answer.clear()
            answer.update(implementation("held"))
            return
        workspace = opened.run.workspaces.candidates[-1]
        handle = asyncio.run_coroutine_threadsafe(
            _submit_failures(
                opened.evaluation,
                workspace,
                opened.evaluation.submitted_revisions[opened.handle],
                kind,
                submissions,
            ),
            loop,
        ).result()[0]
        submissions += 1
        if omitted and submissions == 2:
            asyncio.run_coroutine_threadsafe(
                _submit_failures(
                    opened.evaluation,
                    workspace,
                    opened.evaluation.submitted_revisions[opened.handle],
                    kind,
                    submissions,
                ),
                loop,
            ).result()
            submissions += 1
        opened.evaluation.advance_time(10)
        if return_final and submissions == 4:
            answer.clear()
            answer.update(implementation("held"))
        else:
            answer.update(kind="waiting_for_evaluation", handles=[handle])

    opened = await _open(tmp_path, on_resume=ResumeScript(answer, resumed))
    try:
        task = asyncio.ensure_future(
            PLUGIN.orchestrate(
                opened.runtime,
                dynamic_options(
                    max_in_flight=1,
                    max_rounds=1,
                    judge_every=100,
                    max_retries_per_round=1,
                    max_repeated_failures=3,
                ),
            )
        )
        await opened.waiting(task)
        await _fail_first(opened, kind)
        try:
            await task
        finally:
            assert submissions == 4  # The seed predates this charged attempt.
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert final.workstreams[0].phase is WorkstreamPhase.FAILED
        assert final.workstreams[0].budget.spent == 1
        assert len(final.search.rounds) == 1
        assert len(opened.calls) == (3 if omitted else 4)
        if kind is FailureKind.MEASUREMENT and not omitted:
            assert "not changed the bottleneck" in opened.calls[-1].message
    finally:
        opened.client.close()


async def _fail_first(opened: _Scenario, kind: FailureKind) -> None:
    digest = ContentDigest.sha256(b"immutable capture")
    evidence = TrustedEvidence(
        evidence_id="a" * 64,
        evaluation_id=opened.handle,
        stage_name="benchmark",
        kind=EvidenceKind.BENCHMARK,
        outcome=EvidenceOutcome.FAILED,
        fingerprints=EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
        trusted_inputs=digest,
        accepted_round=0,
        partial_measurement=PartialMeasurement(
            name="warmup_tokens_per_s", value=70, direction="max", unit="tok/s"
        )
        if kind is FailureKind.MEASUREMENT
        else None,
    )
    failure = _TRACEBACK if kind is FailureKind.TRACEBACK else "warmup timed out"
    opened.evaluations.executor.set_state(
        opened.handle,
        EvaluationState.FAILED,
        failure=failure,
        stage_results=(
            EvaluationStepResult(
                name="benchmark",
                state=StageState.SUCCEEDED
                if kind is FailureKind.MEASUREMENT
                else StageState.FAILED,
                failure=failure if kind is FailureKind.TRACEBACK else None,
                result=evidence.model_dump(mode="json")
                if kind is FailureKind.MEASUREMENT
                else None,
            ),
        ),
    )
    await opened.evaluations.coordinator.status(opened.handle)
    report = await opened.evaluations.coordinator.recorded_snapshot(opened.handle)
    opened.evaluation.submitted_reports[opened.handle] = report.model_dump_json()
    opened.evaluation.record_agent_evaluation(
        opened.run.workspaces.candidates[-1], agent_evaluation(report)
    )
