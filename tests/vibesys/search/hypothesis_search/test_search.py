"""Unit and equivalence tests for the ``HypothesisSearch`` facade.

Covers ``start`` (rollback resolution against ``RoundHistory.resolve_rollback_commit``)
and ``close_round`` (round transition, ported from
``loops/multi/decisions.transition_round``) against the old code paths, plus
the parts of the required property suite that are not purely about
``transitions.py``: attempt budgets, frontier non-domination, and rollback
targets always naming an earlier committed round.
"""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from vibesys.agent_run.attempts import AttemptState, JudgeReviewed, JudgeSkipped, JudgeSkipReason
from vibesys.agent_run.evidence import _FAILED_HYPOTHESIS_OUTCOMES as OLD_FAILED_HYPOTHESIS_OUTCOMES
from vibesys.agent_run.evidence import CarryOver as OldCarryOver
from vibesys.agent_run.hypotheses import start_hypothesis as old_start
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.loops.multi import decisions as old_decisions
from vibesys.loops.multi.session import _TerminalPolicy as OldTerminalPolicy
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.schemas import (
    HypothesisOutcome,
    OrchestratorPlan,
)
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch
from vibesys.search.hypothesis.state import HypothesisState
from vibesys.search.hypothesis.transitions import (
    FAILED_HYPOTHESIS_OUTCOMES,
    record_candidate_metrics,
)
from vibesys.search.hypothesis.transitions import CarryOver as NewCarryOver
from vs_loop_state.api import RoundHistory, RoundRecord


def _plan(identifier: str, *, revert_to_round: int | None = None) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        task=f"implement {identifier}",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="test the claim",
        revert_to_round=revert_to_round,
    )


def _round(  # noqa: PLR0913
    number: int,
    *,
    hypothesis_id: str,
    commit: str | None,
    outcome: str | None = "proven",
    parent_round: int | None = None,
    parent_commit: str | None = None,
) -> RoundRecord:
    return RoundRecord(
        round_number=number,
        commit=commit,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        reviewed=True,
        hypothesis_id=hypothesis_id,
        hypothesis_outcome=outcome,
        judge_verdict="pass",
        hypothesis_parent_round=parent_round,
        hypothesis_parent_commit=parent_commit,
    )


# --- attempts: never exceed budget ---


@given(
    retry=st.integers(min_value=1, max_value=20), max_retries=st.integers(min_value=1, max_value=10)
)
def test_attempts_never_exceed_budget(retry: int, max_retries: int) -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5, max_retries_per_round=max_retries))
    budget = search.attempts(retry=retry)
    assert budget.max_retries == max_retries
    assert budget.remaining >= 0
    assert budget.exhausted == (retry > max_retries)


# --- frontier: never dominated ---


def test_frontier_never_dominated() -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="ops", direction="max"),
            Objective(name="latency", direction="min"),
        ),
        relative_noise=0.0,
    )
    records = [
        RoundRecord(
            round_number=n,
            commit=f"{n:040x}",
            passed=True,
            reviewed=True,
            hypothesis_id="H-1",
            hypothesis_outcome="proven",
            judge_verdict="pass",
            official_evaluation=True,
            perf_metric=ops,
            perf_unit="ops",
            perf_direction="max",
            perf_provenance="framework",
            metrics={"ops": ops, "latency": latency},
            candidate_metrics={"ops": ops, "latency": latency},
            candidate_disposition="pareto_frontier",
            candidate_retained=True,
        )
        for n, (ops, latency) in enumerate([(100, 50), (120, 60), (80, 30), (150, 70)], start=1)
    ]
    search = HypothesisSearch(HypothesisConfig(max_rounds=10))
    frontier = search.frontier(records, space=space)

    for candidate in frontier:
        for other in records:
            if other.round_number == candidate.round_number:
                continue
            assert not space.dominates(
                record_candidate_metrics(other), record_candidate_metrics(candidate)
            )


# --- start: rollback target always an earlier committed round ---


def test_start_resolves_rollback_to_earlier_committed_round() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=10))
    state = AgentRunState()
    records = [
        _round(1, hypothesis_id="H-1", commit="a" * 40, outcome="proven"),
        _round(
            2,
            hypothesis_id="H-2",
            commit="b" * 40,
            outcome="disproven",
            parent_round=1,
            parent_commit="a" * 40,
        ),
    ]
    plan = _plan("H-3", revert_to_round=1)
    started = search.start(state, plan, round_number=3, current_commit="c" * 40, records=records)
    assert started.rollback is not None
    assert started.rollback.resolved
    assert started.rollback.commit == "a" * 40
    # The round it rolled back to must be strictly earlier than the round
    # that requested the rollback.
    rollback_round = next(r for r in records if r.commit == started.rollback.commit)
    assert rollback_round.round_number < 3


def test_start_matches_old_resolve_rollback_commit() -> None:
    records = [
        _round(1, hypothesis_id="H-1", commit="a" * 40, outcome="proven"),
        _round(
            2,
            hypothesis_id="H-2",
            commit="b" * 40,
            outcome="disproven",
            parent_round=1,
            parent_commit="a" * 40,
        ),
    ]
    target = records[0]
    old_commit, old_failed_child = RoundHistory(records=records).resolve_rollback_commit(
        target, OLD_FAILED_HYPOTHESIS_OUTCOMES
    )
    new_commit, new_failed_child = RoundHistory(records=records).resolve_rollback_commit(
        target, FAILED_HYPOTHESIS_OUTCOMES
    )
    assert old_commit == new_commit
    assert old_failed_child == new_failed_child


# --- close_round: matches old transition_round ---


def _attempt_state(
    *,
    passed: bool,
    outcome: HypothesisOutcome,
    next_step: str = "keep going",
    feedback: str | None = None,
) -> AttemptState:
    implementation = ImplementerResponse(
        hypothesis_outcome=outcome,
        next_step=next_step,
        summary="did the thing",
        expected_behavior="it works",
    )
    return AttemptState(
        agent_run_state=AgentRunState(),
        feedback=feedback,
        implementation=implementation,
        judge=JudgeReviewed(Verdict.PASS) if passed else JudgeSkipped(JudgeSkipReason.NOT_REACHED),
        passed=passed,
    )


@given(
    passed=st.booleans(),
    reviewed=st.booleans(),
    outcome=st.sampled_from(
        [HypothesisOutcome.CONTINUE, HypothesisOutcome.NOMINATED, HypothesisOutcome.DISPROVEN]
    ),
    continuation_rounds=st.integers(min_value=0, max_value=3),
)
def test_close_round_matches_old_transition_round(
    *, passed: bool, reviewed: bool, outcome: HypothesisOutcome, continuation_rounds: int
) -> None:
    plan = _plan("H-1")
    old_state = old_start(AgentRunState(), plan, started_round=1)
    hypothesis = old_state.active_hypothesis
    assert hypothesis is not None
    hypothesis.continuation_rounds = continuation_rounds

    next_step = "keep going" if outcome is HypothesisOutcome.CONTINUE else ""
    attempt = _attempt_state(
        passed=passed, outcome=outcome, next_step=next_step, feedback="feedback text"
    )
    record = _round(1, hypothesis_id="H-1", commit="a" * 40, outcome=outcome.value)
    record = replace(
        record,
        passed=passed,
        judge_verdict=("pass" if passed else "fail") if reviewed else "deferred",
    )

    policy = OldTerminalPolicy()
    request = old_decisions.TerminalRequest(
        engine=old_decisions.HypothesisEngine.create(old_state),
        state=old_state,
        hypothesis=hypothesis,
        attempt=attempt,
        record=record,
        records=[],
        carry=OldCarryOver(),
        reviewed=reviewed,
        max_retries_per_round=3,
    )
    old_transition = old_decisions.transition_round(policy, request)

    search = HypothesisSearch(HypothesisConfig(max_rounds=5, max_retries_per_round=3))
    keeps_active = policy.keeps_hypothesis_active(attempt, continuation_rounds)
    requests_continuation = old_decisions.implementation_requests_continuation(
        attempt.implementation
    )
    terminal_needs_parent_choice = policy.terminal_success_needs_parent_choice(
        attempt, continuation_rounds
    )

    new_state = HypothesisState.model_validate(old_state.model_dump())
    new_hypothesis = new_state.active_hypothesis
    assert new_hypothesis is not None
    new_hypothesis.continuation_rounds = continuation_rounds
    assert attempt.implementation is not None
    closed = search.close_round(
        new_state,
        hypothesis=new_hypothesis,
        record=record,
        records=[],
        carry=NewCarryOver(),
        passed=passed,
        reviewed=reviewed,
        feedback=attempt.feedback,
        keeps_active=keeps_active,
        requests_continuation=requests_continuation,
        next_step=attempt.implementation.next_step,
        terminal_needs_parent_choice=terminal_needs_parent_choice,
    )

    assert closed.state.model_dump() == old_transition.state.model_dump()
    assert closed.carry.regression_info == old_transition.carry.regression_info
    assert closed.carry.exhaustion_info == old_transition.carry.exhaustion_info
    assert closed.exhaustion_feedback == old_transition.exhaustion_feedback
