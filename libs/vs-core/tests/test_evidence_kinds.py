"""Eligibility names exact closed kinds and preserves source attribution."""

import pytest
from pydantic import ValidationError

import vs_core.api as core


def test_requirements_distinguish_official_correctness_benchmark_from_advisory_evidence() -> None:
    assert hasattr(core, "EvidenceKind")
    requirements = core.EvidenceRequirements(
        required_evidence=(
            core.EvidenceRequirement(
                kind=core.EvidenceKind.CORRECTNESS, provenance="trusted", purpose="official"
            ),
            core.EvidenceRequirement(
                kind=core.EvidenceKind.BENCHMARK, provenance="trusted", purpose="official"
            ),
        ),
        required_assessments=(core.AssessmentKind.CORRECTNESS,),
    )
    assert requirements.required_evidence[0].kind != core.EvidenceKind.PROFILING
    assert requirements.required_evidence[1].kind != core.EvidenceKind.LOCAL_VALIDATION
    assert (
        core.EvidenceRequirements.model_validate_json(requirements.model_dump_json())
        == requirements
    )
    with pytest.raises(ValidationError):
        core.EvidenceRequirement.model_validate_json(
            '{"kind":"anything","provenance":"trusted","purpose":"official"}'
        )


def test_evidence_and_assessments_retain_kind_and_source_ownership() -> None:
    assert hasattr(core, "EvidenceKind")
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    evidence = core.EvidenceRef(
        evidence_id=core.EvidenceId(root="evidence"),
        kind=core.EvidenceKind.BENCHMARK,
        purpose="official",
        scope=scope,
        source_request=core.RequestId(root="request"),
        candidate=state.run.facts.baseline,
        observation_sequence=4,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
    )
    assessment = core.AssessmentProposal(
        kind=core.AssessmentKind.BENCHMARK,
        verdict="satisfied",
        sources=(evidence.evidence_id,),
        candidate=evidence.candidate,
        schema_version=1,
    )
    assert core.EvidenceRef.model_validate_json(evidence.model_dump_json()) == evidence
    assert core.AssessmentProposal.model_validate_json(assessment.model_dump_json()) == assessment
