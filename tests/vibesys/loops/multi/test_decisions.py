"""Multi hypothesis transitions from completed round evidence."""

from __future__ import annotations

import pytest

from vibesys.agent_run.attempts import AttemptState
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.state import AgentRunState
from vibesys.loops.multi.decisions import HypothesisEngine, TerminalRequest, transition_round
from vibesys.loops.multi.session import _TerminalPolicy
from vibesys.schemas import HypothesisOutcome, ImplementerResponse, OrchestratorPlan
from vs_loop_state.api import RoundRecord


@pytest.mark.parametrize(
    ("outcome", "passed", "reviewed", "next_step", "retained", "continuations", "active"),
    [
        (HypothesisOutcome.CONTINUE, True, True, "Finish cache", None, 0, True),
        (HypothesisOutcome.IMPLEMENTATION_FAILED, False, True, "Repair cache", None, 0, True),
        (HypothesisOutcome.SUPPORTED, True, True, "", True, 0, False),
        (HypothesisOutcome.NOMINATED, True, True, "", False, 0, False),
        (HypothesisOutcome.INCONCLUSIVE, False, False, "Measure again", None, 0, True),
        (HypothesisOutcome.IMPLEMENTATION_FAILED, False, True, "Repair cache", None, 2, False),
        (HypothesisOutcome.SUPPORTED, False, False, "", None, 0, False),
    ],
)
def test_completed_round_controls_lease_and_designer_handoff(  # noqa: PLR0913
    outcome: HypothesisOutcome,
    passed: bool,  # noqa: FBT001
    reviewed: bool,  # noqa: FBT001
    next_step: str,
    retained: bool | None,  # noqa: FBT001
    continuations: int,
    active: bool,  # noqa: FBT001
) -> None:
    plan = OrchestratorPlan(
        hypothesis_id="h1",
        hypothesis="Cache decode",
        task="Implement cache",
        pass_criteria="Candidate responds",  # noqa: S106
        reasoning="Decode is expensive",
    )
    engine = HypothesisEngine.create(AgentRunState()).start(
        plan, started_round=1, parent_commit="a" * 40
    )
    state = engine.state
    hypothesis = state.active_hypothesis
    assert hypothesis is not None
    hypothesis.continuation_rounds = continuations
    attempt = AttemptState(agent_run_state=state, feedback="repair needed")
    attempt.implementation = ImplementerResponse(
        summary="Candidate changed",
        expected_behavior="faster",
        hypothesis_outcome=outcome,
        next_step=next_step,
    )
    attempt.passed = passed
    record = RoundRecord(
        round_number=1,
        hypothesis_id="h1",
        commit="b" * 40,
        perf_metric=42.0 if passed else None,
        perf_unit="throughput" if passed else None,
        passed=passed,
        reviewed=reviewed,
        official_evaluation=passed,
        candidate_retained=retained,
    )
    transition = transition_round(
        _TerminalPolicy(),
        TerminalRequest(
            engine=engine,
            state=state,
            hypothesis=hypothesis,
            attempt=attempt,
            record=record,
            records=[],
            carry=CarryOver(regression_info="stale", exhaustion_info="stale"),
            reviewed=reviewed,
            max_retries_per_round=2,
        ),
    )
    assert len(transition.state.rounds) == 1
    assert (transition.state.active_hypothesis is not None) is active
    if reviewed and not passed:
        assert transition.exhaustion_feedback == "repair needed"
        assert "2 attempts" in (transition.carry.exhaustion_info or "")
    else:
        assert transition.exhaustion_feedback is None
        assert transition.carry.exhaustion_info is None
    if passed and outcome is HypothesisOutcome.NOMINATED:
        assert "not retained" in (transition.carry.regression_info or "")
    elif passed and outcome is HypothesisOutcome.SUPPORTED:
        assert transition.carry.regression_info != "stale"
    elif active and not reviewed:
        assert transition.carry.regression_info is None
