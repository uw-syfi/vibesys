"""What an issue-queue run asks of core follows from its config and its roles and nothing else."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.issue_queue.core_policy.api import project_run_snapshot, requirements_for
from vibesys.orchestration.issue_queue.models import IssuePerfEvalResult
from vibesys.orchestration.issue_queue.search.api import (
    IssueBrief,
    IssueQueueConfig,
    QueueSnapshot,
    budget,
    issue_queue,
)
from vibesys.run.contracts import IterationTree
from vs_core.api import AssessmentKind, Capabilities, validate_startup
from vs_sim.api.testing import examples

OPTIONS = st.builds(
    IssueQueueConfig,
    max_rounds=st.integers(1, 6),
    max_attempts_per_issue=st.integers(1, 4),
    max_issues_per_perf_eval=st.integers(1, 4),
    max_issues_per_round=st.none() | st.integers(1, 8),
)
MINIMAL = {"max_rounds": 1, "max_attempts_per_issue": 1, "max_issues_per_perf_eval": 1}


@settings(max_examples=examples(pr=8, nightly=100))
@given(options=OPTIONS)
def test_the_attempt_bound_is_the_rounds_times_the_issues_per_round_and_an_evaluation(
    options: IssueQueueConfig,
) -> None:
    spent = budget(options)

    assert spent.attempts == options.max_rounds * (options.issues_per_round + 1)
    assert spent.parallel == 1
    assert spent.turns >= spent.attempts


@settings(max_examples=examples(pr=8, nightly=100))
@given(options=OPTIONS)
def test_the_default_round_cap_is_twice_the_evaluators_cap_plus_two(
    options: IssueQueueConfig,
) -> None:
    default = options.model_copy(update={"max_issues_per_round": None})

    assert default.issues_per_round == 2 * options.max_issues_per_perf_eval + 2


def test_a_serving_performance_reply_is_read_with_its_load_levels_and_trends() -> None:
    schema = json.dumps(IssuePerfEvalResult.model_json_schema())

    for serving_field in ("load_levels", "token_throughput", "throughput_trend"):
        assert serving_field in schema


def test_a_domain_neutral_performance_reply_names_measurements_and_a_primary_trend() -> None:
    parsed = IssuePerfEvalResult.model_validate_json(
        json.dumps(
            {
                "analysis": "The queue benchmark improved.",
                "metrics": {
                    "measurements": [
                        {"name": "operations_per_second", "value": 42.0, "unit": "ops/s"}
                    ]
                },
                "evaluator_feedback": ["Measure a larger queue."],
                "new_issues": [],
                "primary_metric_trend": "improved",
                "secondary_metric_trend": "mixed",
            }
        )
    )

    assert parsed.primary_trend == "improved"
    assert parsed.secondary_trend == "mixed"


def test_a_reply_that_mixes_the_two_vocabularies_is_refused() -> None:
    with pytest.raises(ValidationError, match="reported with"):
        IssuePerfEvalResult.model_validate_json(
            json.dumps(
                {
                    "analysis": "x",
                    "metrics": {"measurements": []},
                    "evaluator_feedback": [],
                    "throughput_trend": "improved",
                    "latency_trend": "mixed",
                }
            )
        )


def test_the_judge_is_the_declared_authority_for_the_only_required_assessment() -> None:
    requirements = requirements_for()

    (authority,) = requirements.assessment_authorities
    assert authority.kind is AssessmentKind.LOCAL_VALIDATION
    assert authority.role_id == issue_queue.ids.role_id("judge")
    assert requirements.required_assessments == (AssessmentKind.LOCAL_VALIDATION,)


def test_the_declaration_is_served_by_the_operations_the_run_registers() -> None:
    registry = issue_queue.registry()
    assert registry is not None
    offered = Capabilities(lifecycle=frozenset({"suspend"}), operations=registry.descriptors)

    selected = validate_startup(issue_queue.declaration, offered)

    assert selected.operations == registry.descriptors


@pytest.mark.parametrize("field", list(MINIMAL))
def test_a_non_positive_budget_is_refused_naming_the_option(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        IssueQueueConfig.model_validate({**MINIMAL, field: 0})


def test_an_unknown_option_is_refused_naming_the_key() -> None:
    with pytest.raises(ValidationError, match="turn_drop_backoff_seconds"):
        IssueQueueConfig.model_validate({**MINIMAL, "turn_drop_backoff_seconds": 5})


PROJECTED_KEYS = {
    "version",
    "round_idx",
    "cursor",
    "phase",
    "current_issue_id",
    "bootstrap_done",
    "issues",
    "performance",
}


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    round_idx=st.integers(0, 4),
    cursor=st.sampled_from(["drain", "assess", "finished"]),
    current=st.none() | st.integers(1, 5),
    titles=st.lists(st.text(min_size=1, max_size=5), max_size=4),
)
def test_the_projection_keeps_the_fields_clients_have_always_been_given(
    round_idx: int, cursor: str, current: int | None, titles: list[str]
) -> None:
    snapshot = QueueSnapshot(
        round_idx=round_idx,
        cursor=cursor,  # ty: ignore[invalid-argument-type]
        current_issue_id=current,
        issues=tuple(
            IssueBrief(id=n, type="task", status="open", title=title, attempts=0)
            for n, title in enumerate(titles, start=1)
        ),
    )

    payload = project_run_snapshot(snapshot, experiment_revision=0).payload

    assert payload is not None
    assert set(payload) == PROJECTED_KEYS
    assert payload["bootstrap_done"] is bool(titles)
    assert (payload["phase"] is not None) is (current is not None or cursor == "assess")


def test_a_run_that_has_not_filed_its_first_issue_is_pending() -> None:
    projected = project_run_snapshot(
        QueueSnapshot(round_idx=0, cursor="drain"), experiment_revision=0
    )

    assert projected.payload is not None
    assert projected.payload["bootstrap_done"] is False
    assert isinstance(projected.iterations, IterationTree)
    assert projected.iterations.nodes[0].status.value == "pending"
