"""Public policy tests for deterministic evidence preflight."""

from __future__ import annotations

import asyncio
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.evaluation_scenarios import Producer, ScenarioSpec, build_scenario

from vs_evaluation.api import (
    AvailabilitySnapshot,
    AvailabilityState,
    CostClass,
    EvaluationAgentRole,
    EvidenceKind,
    EvidencePreflightResolution,
    ReuseStatus,
    TrustedEvidence,
    decide_evidence_preflight,
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


@cache
def _accepted(kind: EvidenceKind) -> TrustedEvidence:
    """Capture the real producer once; policy properties consume immutable evidence."""

    async def produce() -> TrustedEvidence:
        with TemporaryDirectory(prefix="preflight-evidence-") as directory:
            producer = Producer.SLURM if kind is EvidenceKind.PROFILE else Producer.DIRECT
            async with build_scenario(
                Path(directory), ScenarioSpec(kinds=(kind,)), producer
            ) as scenario:
                return scenario.evidence[0]

    return asyncio.run(produce())


@pytest.fixture(scope="module", autouse=True)
def _capture_accepted_evidence() -> None:
    for kind in EvidenceKind:
        _accepted(kind)


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
