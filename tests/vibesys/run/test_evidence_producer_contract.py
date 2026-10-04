"""Evidence identity and consumer projections from production producers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support.evaluation_scenarios import (
    Producer,
    ScenarioOutcome,
    ScenarioSpec,
    build_scenario,
    scenario_specs,
    slurm_scenario_specs,
)

from vibesys.run.evaluation_backend import SemanticEvaluationStage, agent_evaluation
from vs_evaluation.api import (
    ContentDigest,
    EvaluationAgentRole,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationState,
    EvidenceKind,
    EvidenceOutcome,
    EvidencePreflightResolution,
    PartialMeasurement,
    ResourceRequirements,
    StageState,
    StoredEvaluation,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
    decide_evidence_preflight,
    stable_handle_id,
)
from vs_evaluator_protocol.api import Progress
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentEvaluation,
    AgentEvaluationStatus,
    BenchmarkEvaluation,
    BenchmarkFailureKind,
    MetricDirection,
)


def _assert_projection(
    record: StoredEvaluation,
    submission: SubmittedSemanticEvaluation,
    projection: AgentEvaluation,
) -> tuple[TrustedEvidence, ...]:
    loaded = StoredEvaluation.model_validate_json(record.model_dump_json())
    assert loaded == record
    assert projection == agent_evaluation(loaded)
    assert AgentEvaluation.model_validate_json(projection.model_dump_json()) == projection
    assert projection.content_digest == submission.fingerprints.candidate.value
    first = SemanticEvaluationStage.model_validate(record.request.stages[0].payload)
    assert projection.revision == first.snapshot
    assert projection.kinds == tuple(step.name for step in record.request.stages)
    completed = tuple(
        TrustedEvidence.model_validate(result.result)
        for result in record.stage_results
        if result.state is StageState.SUCCEEDED and result.result is not None
    )
    assert tuple(stage.kind for stage in projection.stages) == tuple(
        evidence.kind.value for evidence in completed
    )
    for stage, evidence in zip(projection.stages, completed, strict=True):
        assert stage.outcome.value == evidence.outcome.value
        assert stage.partial_measurement == evidence.partial_measurement
        assert tuple(metric.model_dump() for metric in stage.metrics) == tuple(
            metric.model_dump() for metric in evidence.metrics
        )
    return completed


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", tuple(Producer))
@pytest.mark.parametrize(
    "kinds",
    [
        (EvidenceKind.ACCURACY,),
        (EvidenceKind.BENCHMARK,),
        (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
    ],
)
@settings(max_examples=12)
@example(spec=ScenarioSpec(outcome=ScenarioOutcome.PASS))
@example(spec=ScenarioSpec(outcome=ScenarioOutcome.CORRECTNESS_FAIL))
@example(spec=ScenarioSpec(outcome=ScenarioOutcome.INFRA_FAIL))
@example(spec=ScenarioSpec(outcome=ScenarioOutcome.TIMEOUT))
@given(spec=scenario_specs())
async def test_producer_identity_and_public_projection_contract(
    producer: Producer, kinds: tuple[EvidenceKind, ...], spec: ScenarioSpec
) -> None:
    spec = replace(spec, kinds=kinds)
    with TemporaryDirectory(prefix="evidence-contract-") as directory:
        async with build_scenario(Path(directory), spec, producer) as scenario:
            record = scenario.record
            submission = scenario.submission
            assert record.handle_id == submission.handle_id == stable_handle_id(record.request.key)
            assert record.handle_id != record.request.key
            assert submission == await scenario.backend.recorded_submission(record.handle_id)
            captures = tuple(
                SemanticEvaluationStage.model_validate(step.payload)
                for step in record.request.stages
            )
            assert tuple(capture.kind for capture in captures) == spec.kinds
            assert all(capture.fingerprints == submission.fingerprints for capture in captures)
            assert await scenario.backend.recorded_snapshot(record.handle_id) == record
            assert submission.fingerprints.candidate == ContentDigest.sha256(
                scenario.candidate_patch.encode()
            )
            for evidence in scenario.evidence:
                assert evidence.evaluation_id == record.handle_id
                assert evidence.evidence_id not in {record.handle_id, record.request.key}
                assert evidence.fingerprints == submission.fingerprints
                assert evidence.trusted_inputs == submission.fingerprints.candidate
                assert evidence.stage_name == evidence.kind.value
                assert evidence.kind in spec.kinds
                assert TrustedEvidence.model_validate_json(evidence.model_dump_json()) == evidence
            assert len({evidence.evidence_id for evidence in scenario.evidence}) == len(
                scenario.evidence
            )
            completed = _assert_projection(record, scenario.submission, scenario.projection)
            assert (
                scenario.projection
                == (await scenario.backend.agent_evaluations((record.handle_id,)))[0]
            )
            if spec.outcome is ScenarioOutcome.PASS:
                assert record.state is EvaluationState.SUCCEEDED
                assert tuple(result.name for result in record.stage_results) == tuple(
                    kind.value for kind in spec.kinds
                )
                assert len(completed) == len(spec.kinds)
            if spec.outcome in {ScenarioOutcome.INFRA_FAIL, ScenarioOutcome.TIMEOUT}:
                assert record.state is EvaluationState.FAILED
                assert not scenario.accepted_evidence
            expected_accepted = completed if record.state is EvaluationState.SUCCEEDED else ()
            assert scenario.accepted_evidence == expected_accepted
            accepted = scenario.accepted_evidence if spec.accepted else ()
            decision = decide_evidence_preflight(
                EvaluationAgentRole.IMPLEMENTER,
                spec.kinds,
                await scenario.backend.availability(ResourceRequirements()),
                accepted,
            )
            assert {
                check.evidence_kind
                for check in decision.checks
                if check.resolution is EvidencePreflightResolution.ACCEPTED
            } == {evidence.kind for evidence in accepted}


@pytest.mark.asyncio
@settings(max_examples=12)
@example(spec=ScenarioSpec(outcome=ScenarioOutcome.PASS))
@example(spec=ScenarioSpec(outcome=ScenarioOutcome.CORRECTNESS_FAIL))
@example(
    spec=ScenarioSpec(
        kinds=(EvidenceKind.BENCHMARK,),
        direction=MetricDirection.MINIMIZE,
        unit="ms",
    )
)
@example(
    spec=ScenarioSpec(
        outcome=ScenarioOutcome.CORRECTNESS_FAIL,
        benchmark_failure=True,
        partial=PartialMeasurement(
            name="warmup_rate",
            value=3.0,
            target=5.0,
            direction="max",
            unit="requests/s",
            progress=Progress(completed=2, required=10, unit="rounds"),
        ),
    )
)
@given(spec=scenario_specs(outcomes=(ScenarioOutcome.PASS, ScenarioOutcome.CORRECTNESS_FAIL)))
async def test_direct_and_slurm_producers_agree_on_shared_semantics(spec: ScenarioSpec) -> None:
    with TemporaryDirectory(prefix="evidence-parity-") as directory:
        root = Path(directory)
        async with (
            build_scenario(root / "direct", spec, Producer.DIRECT) as direct,
            build_scenario(root / "slurm", spec, Producer.SLURM) as slurm,
        ):
            assert direct.submission == slurm.submission
            assert direct.evidence == slurm.evidence
            assert direct.accepted_evidence == slurm.accepted_evidence
            assert direct.projection == slurm.projection
            assert tuple(result.name for result in direct.record.stage_results) == tuple(
                result.name for result in slurm.record.stage_results
            )
            assert direct.record.state == slurm.record.state
            if spec.outcome is ScenarioOutcome.PASS:
                assert len(direct.evidence) == len(spec.kinds)
                assert len(direct.projection.stages) == len(spec.kinds)


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", tuple(Producer))
@settings(max_examples=8)
@given(
    spec=scenario_specs(outcomes=(ScenarioOutcome.PASS,)),
    same_handle=st.booleans(),
)
async def test_replay_preserves_attribution_and_distinct_handles_do_not_alias(
    producer: Producer, spec: ScenarioSpec, *, same_handle: bool
) -> None:
    spec = replace(spec, same_handle=same_handle)
    with TemporaryDirectory(prefix="evidence-replay-") as directory:
        async with build_scenario(Path(directory), spec, producer) as scenario:
            submitted = await scenario.replay()
            assert submitted.fingerprints == scenario.submission.fingerprints
            if same_handle:
                assert submitted.handle_id == scenario.submission.handle_id
                assert (
                    await scenario.backend.recorded_snapshot(submitted.handle_id) == scenario.record
                )
            else:
                assert submitted.handle_id != scenario.submission.handle_id
                replayed = await scenario.backend.recorded_snapshot(submitted.handle_id)
                evidence = tuple(
                    TrustedEvidence.model_validate(result.result)
                    for result in replayed.stage_results
                    if result.result is not None
                )
                assert len(evidence) == len(spec.kinds)
                assert all(item.evaluation_id == submitted.handle_id for item in evidence)
                assert {item.evidence_id for item in evidence}.isdisjoint(
                    item.evidence_id for item in scenario.evidence
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", tuple(Producer))
@settings(max_examples=6)
@example(padding=18_000)
@given(padding=st.integers(min_value=16_370, max_value=32_768))
async def test_long_diagnostics_keep_the_evaluator_cause_as_trusted_failed_evidence(
    producer: Producer, padding: int
) -> None:
    failure = "begin diagnostic\n" + "x" * padding + "\nlast evaluator cause"
    spec = ScenarioSpec(
        outcome=ScenarioOutcome.CORRECTNESS_FAIL,
        kinds=(EvidenceKind.BENCHMARK,),
        benchmark_failure=True,
        failure=failure,
    )
    with TemporaryDirectory(prefix="evidence-long-diagnostic-") as directory:
        async with build_scenario(Path(directory), spec, producer) as scenario:
            assert scenario.record.state is EvaluationState.SUCCEEDED
            assert len(scenario.evidence) == 1
            evidence = scenario.evidence[0]
            assert evidence.outcome is EvidenceOutcome.FAILED
            assert evidence.semantic_summary is not None
            assert failure.endswith(evidence.semantic_summary)
            assert "last evaluator cause" in evidence.semantic_summary
            assert scenario.accepted_evidence == scenario.evidence
            assert scenario.projection.status is AgentEvaluationStatus.FAILED
            assert "last evaluator cause" in (scenario.projection.failure or "")


class _BenchmarkFault(StrEnum):
    TYPED_INFRASTRUCTURE = "typed_infrastructure"
    EXCEPTION = "exception"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", tuple(_BenchmarkFault))
@given(executed=st.booleans(), with_feedback=st.booleans())
async def test_later_infrastructure_failure_retains_prior_stage_without_trusting_failure(
    fault: _BenchmarkFault, *, executed: bool, with_feedback: bool
) -> None:
    spec = ScenarioSpec()
    with TemporaryDirectory(prefix="evidence-infrastructure-") as directory:
        async with build_scenario(Path(directory), spec, Producer.DIRECT) as scenario:
            scenario.run.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
            failure = "benchmark infrastructure failed"
            if fault is _BenchmarkFault.TYPED_INFRASTRUCTURE:
                scenario.run.evaluation.script_benchmark(
                    BenchmarkEvaluation(
                        executed=executed,
                        feedback=failure if with_feedback else None,
                        failure_kind=BenchmarkFailureKind.INFRASTRUCTURE,
                    )
                )
            elif fault is _BenchmarkFault.CANCELED:
                scenario.run.evaluation.script_benchmark(asyncio.CancelledError(failure))
            else:
                error = (
                    OSError(failure)
                    if fault is _BenchmarkFault.EXCEPTION
                    else TimeoutError(failure)
                )
                scenario.run.evaluation.script_benchmark(error)
            first = SemanticEvaluationStage.model_validate(
                scenario.record.request.stages[0].payload
            )
            submission = await scenario.backend.submit_revision_evidence(
                first.snapshot, spec.kinds, scope_id="infrastructure-failure"
            )
            canceled = fault is _BenchmarkFault.CANCELED
            assert isinstance(
                await scenario.backend.await_result(submission.handle_id, 60),
                EvaluationCanceled if canceled else EvaluationFailed,
            )
            record = await scenario.backend.recorded_snapshot(submission.handle_id)
            assert record.state is (
                EvaluationState.CANCELED if canceled else EvaluationState.FAILED
            )
            assert record.stage_results
            assert record.stage_results[0].name == EvidenceKind.ACCURACY.value
            assert record.stage_results[0].state is StageState.SUCCEEDED
            prior = TrustedEvidence.model_validate(record.stage_results[0].result)
            assert prior.evaluation_id == submission.handle_id
            (projection,) = await scenario.backend.agent_evaluations((submission.handle_id,))
            assert projection.status is (
                AgentEvaluationStatus.CANCELED if canceled else AgentEvaluationStatus.FAILED
            )
            assert tuple(stage.kind for stage in projection.stages) == (
                EvidenceKind.ACCURACY.value,
            )
            operation = await scenario.backend.operation_snapshot(submission.handle_id)
            assert operation.evidence_ids == (prior.evidence_id,)
            assert not operation.evidence_recorded
            accepted = await scenario.backend.evidence_for(scenario.workspace, spec.kinds)
            assert all(item.evaluation_id != submission.handle_id for item in accepted)
            if fault is _BenchmarkFault.TYPED_INFRASTRUCTURE:
                diagnostic = record.stage_results[1]
                assert diagnostic.name == EvidenceKind.BENCHMARK.value
                assert diagnostic.state is StageState.FAILED
                diagnostic_evidence = TrustedEvidence.model_validate(diagnostic.result)
                assert diagnostic_evidence.evaluation_id == submission.handle_id
                assert diagnostic_evidence.outcome is EvidenceOutcome.FAILED


@pytest.mark.asyncio
@given(executed=st.booleans(), failed=st.booleans())
async def test_execution_flag_does_not_override_a_semantic_benchmark_outcome(
    *, executed: bool, failed: bool
) -> None:
    spec = ScenarioSpec(kinds=(EvidenceKind.BENCHMARK,))
    with TemporaryDirectory(prefix="evidence-execution-flag-") as directory:
        async with build_scenario(Path(directory), spec, Producer.DIRECT) as scenario:
            scenario.run.evaluation.script_benchmark(
                BenchmarkEvaluation(
                    executed=executed,
                    feedback="workload rejected" if failed else None,
                    failure_kind=BenchmarkFailureKind.WORKLOAD if failed else None,
                )
            )
            capture = SemanticEvaluationStage.model_validate(
                scenario.record.request.stages[0].payload
            )
            submission = await scenario.backend.submit_revision_evidence(
                capture.snapshot, spec.kinds, scope_id="semantic-outcome"
            )
            assert isinstance(
                await scenario.backend.await_result(submission.handle_id, 60), EvaluationCompleted
            )
            record = await scenario.backend.recorded_snapshot(submission.handle_id)
            evidence = TrustedEvidence.model_validate(record.stage_results[0].result)
            assert evidence.outcome is (
                EvidenceOutcome.FAILED if failed else EvidenceOutcome.PASSED
            )
            accepted = await scenario.backend.evidence_for(scenario.workspace, spec.kinds)
            assert evidence in accepted


@pytest.mark.asyncio
@settings(max_examples=6)
@given(metric=st.integers(min_value=1, max_value=1000).map(float))
async def test_cancellation_preserves_completed_stage_evidence(metric: float) -> None:
    spec = ScenarioSpec(metric=metric)
    with TemporaryDirectory(prefix="evidence-cancellation-") as directory:
        async with build_scenario(Path(directory), spec, Producer.DIRECT) as scenario:
            gate = scenario.run.evaluation.gate(
                "benchmark", len(scenario.run.evaluation.benchmark_calls)
            )
            scenario.run.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
            capture = SemanticEvaluationStage.model_validate(
                scenario.record.request.stages[0].payload
            )
            submission = await scenario.backend.submit_revision_evidence(
                capture.snapshot, spec.kinds, scope_id="cancellation"
            )
            await gate.entered.wait()
            running = await scenario.backend.inspect_snapshot(submission.handle_id)
            assert running is not None
            assert len(running.stage_results) == 1
            prior = TrustedEvidence.model_validate(running.stage_results[0].result)
            canceled = await scenario.backend.cancel(submission.handle_id)
            assert canceled.state is EvaluationState.CANCELED
            assert canceled.stage_results == running.stage_results
            (projection,) = await scenario.backend.agent_evaluations((submission.handle_id,))
            assert projection.status is AgentEvaluationStatus.CANCELED
            assert tuple(stage.kind for stage in projection.stages) == (
                EvidenceKind.ACCURACY.value,
            )
            operation = await scenario.backend.operation_snapshot(submission.handle_id)
            assert operation.evidence_ids == (prior.evidence_id,)
            assert not operation.evidence_recorded


@pytest.mark.asyncio
@settings(max_examples=12)
@example(spec=ScenarioSpec(outcome=ScenarioOutcome.PASS, scheduler_failed=True))
@example(
    spec=ScenarioSpec(
        outcome=ScenarioOutcome.CORRECTNESS_FAIL,
        benchmark_failure=True,
        scheduler_failed=True,
    )
)
@given(spec=slurm_scenario_specs())
async def test_slurm_aggregate_failure_is_independent_of_semantic_stage_verdicts(
    spec: ScenarioSpec,
) -> None:
    with TemporaryDirectory(prefix="evidence-aggregate-") as directory:
        async with build_scenario(Path(directory), spec, Producer.SLURM) as scenario:
            record = scenario.record
            completed = _assert_projection(record, scenario.submission, scenario.projection)
            assert tuple(result.name for result in record.stage_results) == tuple(
                kind.value for kind in spec.kinds
            )
            assert all(
                evidence.evaluation_id == scenario.submission.handle_id
                and evidence.fingerprints == scenario.submission.fingerprints
                for evidence in scenario.evidence
            )
            if spec.scheduler_failed:
                assert record.state is EvaluationState.FAILED
                assert not scenario.accepted_evidence
                assert scenario.projection.status is AgentEvaluationStatus.FAILED
            else:
                expected_state = (
                    EvaluationState.FAILED
                    if spec.outcome is ScenarioOutcome.CORRECTNESS_FAIL
                    and not spec.benchmark_failure
                    and spec.kinds == (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
                    else EvaluationState.SUCCEEDED
                )
                assert record.state is expected_state
                assert scenario.accepted_evidence == (
                    completed if expected_state is EvaluationState.SUCCEEDED else ()
                )
            if spec.outcome is ScenarioOutcome.PASS:
                assert len(scenario.evidence) == len(spec.kinds)
                assert all(item.outcome is EvidenceOutcome.PASSED for item in scenario.evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kinds",
    [
        (EvidenceKind.BENCHMARK,),
        (EvidenceKind.BENCHMARK, EvidenceKind.ACCURACY),
    ],
)
@given(executed=st.booleans(), with_feedback=st.booleans())
async def test_first_infrastructure_failure_does_not_trust_or_execute_successor_stages(
    kinds: tuple[EvidenceKind, ...], *, executed: bool, with_feedback: bool
) -> None:
    spec = ScenarioSpec(kinds=kinds)
    with TemporaryDirectory(prefix="evidence-first-infrastructure-") as directory:
        async with build_scenario(Path(directory), spec, Producer.DIRECT) as scenario:
            scenario.run.evaluation.script_benchmark(
                BenchmarkEvaluation(
                    executed=executed,
                    feedback="benchmark infrastructure failed" if with_feedback else None,
                    failure_kind=BenchmarkFailureKind.INFRASTRUCTURE,
                )
            )
            capture = SemanticEvaluationStage.model_validate(
                scenario.record.request.stages[0].payload
            )
            submitted = await scenario.backend.submit_revision_evidence(
                capture.snapshot, kinds, scope_id="first-infrastructure"
            )
            assert isinstance(
                await scenario.backend.await_result(submitted.handle_id, 60), EvaluationFailed
            )
            record = await scenario.backend.recorded_snapshot(submitted.handle_id)
            assert record.state is EvaluationState.FAILED
            assert tuple(result.name for result in record.stage_results) == tuple(
                kind.value for kind in kinds
            )
            diagnostic = record.stage_results[0]
            assert diagnostic.state is StageState.FAILED
            evidence = TrustedEvidence.model_validate(diagnostic.result)
            assert evidence.evaluation_id == submitted.handle_id
            assert evidence.evidence_id != submitted.handle_id
            assert evidence.fingerprints == submitted.fingerprints
            assert evidence.outcome is EvidenceOutcome.FAILED
            assert all(
                result.state is StageState.SKIPPED and result.result is None
                for result in record.stage_results[1:]
            )
            (projection,) = await scenario.backend.agent_evaluations((submitted.handle_id,))
            assert projection.status is AgentEvaluationStatus.FAILED
            assert not projection.stages
            operation = await scenario.backend.operation_snapshot(submitted.handle_id)
            assert not operation.evidence_ids
            assert not operation.evidence_recorded
            accepted = await scenario.backend.evidence_for(scenario.workspace, kinds)
            assert all(item.evaluation_id != submitted.handle_id for item in accepted)
