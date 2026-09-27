"""Public policy tests for deterministic evidence preflight."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vibesys.evaluation_agent.api import (
    ContentDigest,
    EvaluationAgentRole,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    EvidencePreflightResolution,
    TrustedEvidence,
    decide_evidence_preflight,
)
from vs_evaluation.api import (
    AvailabilitySnapshot,
    AvailabilityState,
    CostClass,
    ReuseStatus,
)


def _availability(
    state: AvailabilityState,
    supported: tuple[EvidenceKind, ...],
) -> AvailabilitySnapshot:
    return AvailabilitySnapshot(
        state=state,
        capacity=1,
        in_flight=0,
        queue_depth=0,
        reuse_status=ReuseStatus.NONE,
        cost_class=CostClass.UNKNOWN,
        observed_at=0.0,
        fresh_for_s=60.0,
        supported_evidence_kinds=tuple(kind.value for kind in supported),
    )


def _accepted(kind: EvidenceKind) -> TrustedEvidence:
    digest = ContentDigest.sha256(b"preflight")
    return TrustedEvidence(
        evidence_id="a" * 64,
        evaluation_id="evaluation",
        stage_name=kind.value,
        kind=kind,
        fingerprints=EvidenceFingerprints(
            candidate=digest,
            evaluator=digest,
            workload=digest,
            environment=digest,
        ),
        trusted_inputs=digest,
        outcome=(
            EvidenceOutcome.OBSERVED
            if kind is not EvidenceKind.ACCURACY
            else EvidenceOutcome.PASSED
        ),
        accepted_round=1,
    )


@given(
    state=st.sampled_from(AvailabilityState),
    evidence_kind=st.sampled_from(EvidenceKind),
    supported=st.sets(st.sampled_from(EvidenceKind)).map(tuple),
)
def test_preflight_resolution_is_a_pure_function_of_authority_and_availability(
    state: AvailabilityState,
    evidence_kind: EvidenceKind,
    supported: tuple[EvidenceKind, ...],
) -> None:
    decision = decide_evidence_preflight(
        EvaluationAgentRole.IMPLEMENTER,
        (evidence_kind,),
        _availability(state, supported),
        (),
    )

    if evidence_kind is EvidenceKind.PROFILE:
        expected = EvidencePreflightResolution.UNAUTHORIZED
    elif evidence_kind not in supported:
        expected = EvidencePreflightResolution.UNSUPPORTED
    elif state is AvailabilityState.UNAVAILABLE:
        expected = EvidencePreflightResolution.UNAVAILABLE
    else:
        expected = EvidencePreflightResolution.COLLECTABLE
    assert decision.checks[0].resolution is expected
    assert decision.blocked is (expected is not EvidencePreflightResolution.COLLECTABLE)


def test_delegated_profile_is_collectable_without_direct_submission_authority() -> None:
    decision = decide_evidence_preflight(
        EvaluationAgentRole.IMPLEMENTER,
        (EvidenceKind.PROFILE,),
        _availability(AvailabilityState.IMMEDIATE, (EvidenceKind.PROFILE,)),
        (),
        delegated_evidence_kinds=frozenset({EvidenceKind.PROFILE}),
    )

    assert decision.blocked is False
    assert decision.checks[0].resolution is EvidencePreflightResolution.COLLECTABLE


@given(
    state=st.sampled_from(AvailabilityState),
    evidence_kind=st.sampled_from(EvidenceKind),
)
def test_exact_accepted_evidence_satisfies_preflight_before_resource_checks(
    state: AvailabilityState,
    evidence_kind: EvidenceKind,
) -> None:
    decision = decide_evidence_preflight(
        EvaluationAgentRole.IMPLEMENTER,
        (evidence_kind,),
        _availability(state, ()),
        (_accepted(evidence_kind),),
    )

    assert decision.blocked is False
    assert decision.checks[0].resolution is EvidencePreflightResolution.ACCEPTED
