"""The core policy of an issue-queue run follows from its options and nothing else."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.issue_queue.core_policy.api import (
    PolicyInputs,
    RunBounds,
    build_core_policy,
)
from vibesys.orchestration.issue_queue.models import IssueQueueOptions
from vibesys.orchestration.issue_queue.strategy.api import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PERF_REPLY,
    plain_operation_registry,
    role_id,
)
from vs_core.api import ArtifactId, ArtifactRef, AssessmentKind, Capabilities, validate_startup

BOUNDS = RunBounds(
    queue_allowance_seconds=900, observe_interval_seconds=10, observe_backoff_cap_seconds=120
)
INPUTS = PolicyInputs(
    objective="serve it",
    baseline_commit="abc123",
    evaluator_digest="e",
    workload_digest="w",
    environment_digest="env",
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
    accuracy_configured=True,
    benchmark_configured=True,
)
OPTIONS = st.builds(
    IssueQueueOptions,
    max_rounds=st.integers(1, 6),
    max_attempts_per_issue=st.integers(1, 4),
    max_issues_per_perf_eval=st.integers(1, 4),
    max_issues_per_round=st.none() | st.integers(1, 8),
)


@given(options=OPTIONS)
def test_the_attempt_bound_is_the_rounds_times_the_issues_per_round_and_an_evaluation(
    options: IssueQueueOptions,
) -> None:
    policy = build_core_policy(options, BOUNDS, INPUTS)
    per_round = options.issues_per_round
    assert policy.limits.max_attempts == options.max_rounds * (per_round + 1)
    assert policy.limits.max_parallel == 1
    assert policy.limits.max_turns >= policy.limits.max_attempts


@given(options=OPTIONS)
def test_the_default_round_cap_is_twice_the_evaluators_cap_plus_two(
    options: IssueQueueOptions,
) -> None:
    default = options.model_copy(update={"max_issues_per_round": None})
    assert default.issues_per_round == 2 * options.max_issues_per_perf_eval + 2


@given(options=OPTIONS)
def test_every_declared_reply_schema_has_a_reply_type_and_the_evaluators_is_capped(
    options: IssueQueueOptions,
) -> None:
    policy = build_core_policy(options, BOUNDS, INPUTS)
    assert set(policy.reply_schemas) == {IMPLEMENTER_REPLY, JUDGE_REPLY, PERF_REPLY}
    cap = options.max_issues_per_perf_eval
    capped = policy.reply_schemas[PERF_REPLY]
    schema = json.dumps(capped.model_json_schema())
    assert f'"maxItems": {cap}' in schema


def test_the_judge_is_the_declared_authority_for_the_only_required_assessment() -> None:
    policy = build_core_policy(
        IssueQueueOptions(max_rounds=1, max_attempts_per_issue=1, max_issues_per_perf_eval=1),
        BOUNDS,
        INPUTS,
    )
    (authority,) = policy.requirements.assessment_authorities
    assert authority.kind is AssessmentKind.LOCAL_VALIDATION
    assert authority.role_id == role_id("judge")
    assert policy.requirements.required_assessments == (AssessmentKind.LOCAL_VALIDATION,)


def test_the_declaration_is_served_by_the_operations_the_run_registers() -> None:
    policy = build_core_policy(
        IssueQueueOptions(max_rounds=1, max_attempts_per_issue=1, max_issues_per_perf_eval=1),
        BOUNDS,
        INPUTS,
    )
    offered = Capabilities(
        lifecycle=frozenset({"suspend"}), operations=plain_operation_registry().descriptors
    )
    selected = validate_startup(policy.strategy.declaration, offered)
    assert selected.operations == plain_operation_registry().descriptors


@pytest.mark.parametrize(
    "field", ["max_rounds", "max_attempts_per_issue", "max_issues_per_perf_eval"]
)
def test_a_non_positive_budget_is_refused_naming_the_option(field: str) -> None:
    values = {"max_rounds": 1, "max_attempts_per_issue": 1, "max_issues_per_perf_eval": 1}
    values[field] = 0
    with pytest.raises(ValidationError, match=field):
        IssueQueueOptions.model_validate(values)
