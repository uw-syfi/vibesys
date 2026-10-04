"""Measurement contracts retain required fields instead of promising aggregate answers."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.dynamic import DynamicState, ProfilePlan
from vibesys.orchestration.dynamic.models import DynamicProfile, WorkstreamKind
from vibesys.orchestration.dynamic.profiles import profile_requirements, unavailable_profile_fields
from vs_runtime.api import CandidateProfile, CandidateProfileStatus, ProfileField
from vs_runtime.api.testing import FakeEvaluation

_FIELDS = st.lists(st.sampled_from(ProfileField), unique=True).map(tuple)


@given(required=_FIELDS, supported=_FIELDS)
def test_fake_cannot_script_away_missing_measurement_fields(
    required: tuple[ProfileField, ...], supported: tuple[ProfileField, ...]
) -> None:
    evaluation = FakeEvaluation(profiling_supported=True, supported_profile_fields=supported)
    evaluation.script_profile(
        CandidateProfile(
            revision="r",
            status=CandidateProfileStatus.OBSERVED,
            diagnosis="aggregate",
        )
    )
    outcome = asyncio.run(evaluation.profile("r", "q", member_id="p", required_fields=required))
    missing = tuple(field for field in required if field not in supported)
    assert outcome.missing_fields == missing
    assert outcome.status is (
        CandidateProfileStatus.UNSUPPORTED if missing else CandidateProfileStatus.OBSERVED
    )


@given(fields=_FIELDS)
def test_explicit_requirements_survive_the_public_plan(fields: tuple[ProfileField, ...]) -> None:
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="p",
        target_hypothesis_id=None,
        question="Measure the cost",
        required_fields=fields,
    )
    assert set(profile_requirements(plan)) == set(fields)


@pytest.mark.parametrize(
    "question",
    [
        "prefill/decode split and HIP API timing",
        "PREFILL and DECODE, HIP-runtime overhead",
        "prefill_decode and hip_api durations",
    ],
)
def test_historical_questions_preserve_named_measurements(question: str) -> None:
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE, profile_id="p", target_hypothesis_id=None, question=question
    )
    assert set(profile_requirements(plan)) == set(ProfileField)


def test_unknown_measurement_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="required_fields"):
        ProfilePlan(
            kind=WorkstreamKind.PROFILE,
            profile_id="p",
            target_hypothesis_id=None,
            question="Measure",
            required_fields=["invented"],
        )


def test_observed_aggregate_cannot_name_missing_fields() -> None:
    with pytest.raises(ValidationError, match="unsupported"):
        CandidateProfile(
            revision="r",
            status=CandidateProfileStatus.OBSERVED,
            diagnosis="aggregate",
            missing_fields=(ProfileField.DECODE_TIMING,),
        )


@pytest.mark.parametrize(
    "question",
    [
        "Measure decoder kernels and chip latency.",
        "Measure ownership APIs and prefilled-cache kernels.",
        "Compare hipster APIs and xprefill decoder kernels.",
    ],
)
def test_historical_bridge_does_not_infer_fields_from_unrelated_words(question: str) -> None:
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE, profile_id="p", target_hypothesis_id=None, question=question
    )
    assert profile_requirements(plan) == ()


@given(
    term=st.sampled_from(["prefill", "decode", "hip"]),
    prefix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
)
def test_measurement_names_embedded_in_other_words_are_not_requirements(
    term: str, prefix: str, suffix: str
) -> None:
    question = f"Measure {prefix}{term}{suffix} API cost"
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE, profile_id="p", target_hypothesis_id=None, question=question
    )
    assert profile_requirements(plan) == ()


@pytest.mark.parametrize(
    "question",
    [
        "pre-fill and de-code timing, HIP APIs",
        "prefilling and decoding timing with HIP-runtime calls",
    ],
)
def test_historical_bridge_accepts_named_phase_variants(question: str) -> None:
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE, profile_id="p", target_hypothesis_id=None, question=question
    )
    assert set(profile_requirements(plan)) == set(ProfileField)


@given(requested=_FIELDS, unavailable=_FIELDS)
def test_only_known_missing_fields_are_removed_from_future_requests(
    requested: tuple[ProfileField, ...], unavailable: tuple[ProfileField, ...]
) -> None:
    prior = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="prior",
        target_hypothesis_id=None,
        question="Measure",
    )
    state = DynamicState(
        profiles=[
            DynamicProfile(
                profile_id="prior",
                sequence=1,
                planning_call=1,
                plan=prior,
                revision="old-revision",
                outcome=CandidateProfile(
                    revision="old-revision",
                    status=CandidateProfileStatus.UNSUPPORTED,
                    missing_fields=unavailable,
                    diagnosis="Unavailable",
                ),
            )
        ]
    )
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="next",
        target_hypothesis_id=None,
        question="Measure",
        required_fields=requested,
    )
    assert set(unavailable_profile_fields(state, plan)) == set(requested) & set(unavailable)
    assert state.unsupported_profiles() == 1
    assert state.unsupported_profiles(scope="capability") == (0 if unavailable else 1)
