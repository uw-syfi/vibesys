"""A process that dies at any durable boundary is replaced by one that finishes the same run.

The reference run commits some number of times and runs some number of effects. For a death after
any of those commits, and for a death after any effect ran but before its observation was
committed (the board write that landed whose acknowledgement was lost, among them), a new
process over the same store reaches the same result and the same board, with no request
executed twice.
"""

from __future__ import annotations

from collections import deque

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors
from tests.vibesys.orchestration.issue_queue.strategy._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.strategy._world import (
    DiesAt,
    Outside,
    run_with_restarts,
)

from vs_core.api import RunStatus


def _script() -> Executors:
    """Two issues: the bootstrap one fails its first review, and its judge finds a bug."""
    return Executors(
        implementer=deque(implemented(f"turn {n}") for n in range(6)),
        judge=deque([judged(passed=False), judged(bugs=("a bug",)), judged(), judged()]),
        perf=deque([evaluated(), evaluated()]),
    )


def _board(script: Executors) -> list[tuple[object, ...]]:
    return [
        (issue.id, issue.status.value, issue.attempts, [e.action for e in issue.history])
        for issue in script.tracker.list()
    ]


def _reference() -> tuple[Outside, Executors, object]:
    outside = Outside()
    script = _script()
    core = run_with_restarts(script, outside, [])
    assert core.run.status == RunStatus.TERMINAL
    return outside, script, core.run.result


REFERENCE = _reference()
COMMITS = REFERENCE[0].commits
EFFECTS = len(REFERENCE[0].executed)


def _finish(dies: list[DiesAt]) -> tuple[Outside, Executors, object]:
    outside = Outside()
    script = _script()
    core = run_with_restarts(script, outside, dies)
    assert core.run.status == RunStatus.TERMINAL
    return outside, script, core.run.result


def _check(outside: Outside, script: Executors, result: object, deaths: int) -> None:
    assert outside.deaths == deaths, "the process died where it was told to"
    reference_outside, reference_script, reference_result = REFERENCE
    assert result == reference_result
    assert _board(script) == _board(reference_script)
    assert len(outside.executed) == len(set(outside.executed)), "a request executed twice"
    assert {d.decision_id.root for d in outside.decisions} == {
        d.decision_id.root for d in reference_outside.decisions
    }


@settings(max_examples=8, deadline=None, derandomize=True, database=None)
@given(commit=st.integers(min_value=1, max_value=COMMITS))
def test_a_death_after_any_commit_is_recovered_to_the_same_run(commit: int) -> None:
    _check(*_finish([DiesAt(commit=commit)]), deaths=1)


@settings(max_examples=8, deadline=None, derandomize=True, database=None)
@given(effect=st.integers(min_value=1, max_value=EFFECTS))
def test_a_death_after_any_effect_before_its_observation_is_recovered_to_the_same_run(
    effect: int,
) -> None:
    _check(*_finish([DiesAt(effect=effect)]), deaths=1)


@settings(max_examples=8, deadline=None, derandomize=True, database=None)
@given(
    first=st.integers(min_value=1, max_value=COMMITS),
    second=st.integers(min_value=1, max_value=40),
)
def test_a_second_death_during_recovery_is_recovered_too(first: int, second: int) -> None:
    outside, script, result = _finish([DiesAt(commit=first), DiesAt(commit=second)])
    _check(outside, script, result, deaths=outside.deaths)
    assert outside.deaths >= 1
