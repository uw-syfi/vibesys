"""Evidence identity and consumer projections from production producers."""

from __future__ import annotations

from dataclasses import replace
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
)

from vibesys.run.evaluation_backend import SemanticEvaluationStage, agent_evaluation
from vs_evaluation.api import (
    ContentDigest,
    EvaluationAgentRole,
    EvaluationState,
    EvidenceKind,
    EvidencePreflightResolution,
    ResourceRequirements,
    StageState,
    StoredEvaluation,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
    decide_evidence_preflight,
    stable_handle_id,
)
from vs_runtime.api import AgentEvaluation


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
