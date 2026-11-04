"""Evaluations have no allowance; an egregious count is warned of, once, and never refused (#3047).

An agent may ask for as many evaluations as it likes. Core only records, once per attempt, that
the count reached `TaskTerms.runaway_evaluations`; nothing is refused or waits because of it.
These tests drive the kernel through the agent's public evaluation call.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import vs_core.api as core

from .test_measurement_comparison import Lab
from .test_measurement_reuse import submissions


def lab_with(limit: int, *attempts: str) -> Lab:
    lab = Lab.of(*attempts)
    task = lab.state.run.facts.task
    assert task is not None
    facts = lab.state.run.facts.model_copy(
        update={"task": task.model_copy(update={"runaway_evaluations": limit})}
    )
    lab.state = lab.state.model_copy(
        update={"run": lab.state.run.model_copy(update={"facts": facts})}
    )
    return lab


def evaluate_distinct(lab: Lab, name: str, count: int, *, first: int = 0) -> int:
    """The attempt asks for ``count`` evaluations of different candidates; how many were admitted."""
    admitted = 0
    for index in range(first, first + count):
        asked = lab.call(name, f"commit-{name}-{index}", f"input-{name}-{index}")
        admitted += any(r.plan.purpose == "local-validation" for r in asked)
    return admitted


@given(limit=st.integers(1, 6), calls=st.integers(1, 12))
@settings(deadline=None, max_examples=40)
def test_an_attempt_is_warned_of_once_when_its_count_reaches_the_limit_and_nothing_is_refused(
    limit: int, calls: int
) -> None:
    lab = lab_with(limit, "a")
    admitted = evaluate_distinct(lab, "a", calls)
    runaways = lab.state.evaluation.runaways
    assert admitted == calls
    assert all(call.rejection is None for call in lab.state.evaluation.agent_calls)
    if calls < limit:
        assert runaways == ()
    else:
        assert runaways == (core.Runaway(scope=lab.scopes["a"], evaluations=limit, limit=limit),)


def test_each_attempt_is_counted_on_its_own() -> None:
    lab = lab_with(3, "a", "b")
    evaluate_distinct(lab, "a", 2)
    evaluate_distinct(lab, "b", 2)
    assert lab.state.evaluation.runaways == ()
    evaluate_distinct(lab, "b", 1, first=2)
    assert [r.scope for r in lab.state.evaluation.runaways] == [lab.scopes["b"]]


def test_asking_again_for_a_candidate_already_evaluated_is_not_another_evaluation() -> None:
    lab = lab_with(2, "a")
    lab.call("a", "commit-1", "input-1")
    lab.call("a", "commit-1", "input-1")
    assert lab.state.evaluation.runaways == ()
    lab.call("a", "commit-2", "input-2")
    assert len(lab.state.evaluation.runaways) == 1


def test_the_default_limit_is_a_count_of_dozens_never_a_handful() -> None:
    assert core.TaskTerms.model_fields["runaway_evaluations"].default >= 20


def test_a_task_cannot_declare_a_limit_below_one() -> None:
    task = Lab.of("a").state.run.facts.task
    assert task is not None
    with pytest.raises(ValidationError):
        core.TaskTerms.model_validate({**task.model_dump(), "runaway_evaluations": 0})


def test_a_runaway_does_not_change_what_core_asks_the_executor_to_run() -> None:
    lab = lab_with(1, "a")
    asked = lab.call("a", "commit-1", "input-1")
    assert [r.plan.purpose for r in asked] == ["local-validation", "baseline"]
    assert len(lab.state.evaluation.runaways) == 1
    assert len(submissions(lab.state)) == 2
