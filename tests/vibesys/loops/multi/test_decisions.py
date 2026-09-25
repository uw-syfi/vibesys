"""Multi hypothesis transitions from completed round evidence."""

from __future__ import annotations

import pytest

from vibesys.loops.multi.session import _TerminalPolicy
from vibesys.roles.implementer import ImplementerResponse
from vibesys.schemas import HypothesisOutcome
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch, OrchestratorPlan
from vibesys.search.hypothesis.attempts import AttemptState
from vibesys.search.hypothesis.transitions import CarryOver
from vibesys.search.profile_focus import ProfileBottleneck, ProfileFocus, ProfileFocusConfig
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
    search = HypothesisSearch(HypothesisConfig(max_rounds=5, max_retries_per_round=2))
    started = search.start(
        search.initial(), plan, round_number=1, current_commit="a" * 40, records=[]
    )
    state = started.state
    hypothesis = started.hypothesis
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
    policy = _TerminalPolicy(search.config)
    keeps_active = policy.keeps_hypothesis_active(attempt, continuations)
    terminal_needs_parent_choice = policy.terminal_success_needs_parent_choice(
        attempt, continuations
    )
    closed = search.close_round(
        state,
        hypothesis=hypothesis,
        record=record,
        records=[],
        carry=CarryOver(regression_info="stale", exhaustion_info="stale"),
        passed=passed,
        reviewed=reviewed,
        feedback=attempt.feedback,
        keeps_active=keeps_active,
        requests_continuation=bool(
            outcome
            in {
                HypothesisOutcome.CONTINUE,
                HypothesisOutcome.IMPLEMENTATION_FAILED,
                HypothesisOutcome.INCONCLUSIVE,
            }
            and next_step.strip()
        ),
        next_step=next_step,
        terminal_needs_parent_choice=terminal_needs_parent_choice,
    )
    assert len(closed.state.rounds) == 1
    assert (closed.state.active_hypothesis is not None) is active
    if reviewed and not passed:
        assert closed.exhaustion_feedback == "repair needed"
        assert "2 attempts" in (closed.carry.exhaustion_info or "")
    else:
        assert closed.exhaustion_feedback is None
        assert closed.carry.exhaustion_info is None
    if passed and outcome is HypothesisOutcome.NOMINATED:
        assert "not retained" in (closed.carry.regression_info or "")
    elif passed and outcome is HypothesisOutcome.SUPPORTED:
        assert closed.carry.regression_info != "stale"
    elif active and not reviewed:
        assert closed.carry.regression_info is None


def test_completed_round_advances_profile_focus_cursor() -> None:
    """When profiling is on, session.commit_round also advances ``ProfileFocus``.

    ``search.hypothesis.close_round`` and ``search.profile_focus.record`` are
    independent; ``session.commit_round`` composes both from one
    ``RoundRecord``, mirrored here directly against the pure policy.
    """
    focus = ProfileFocus(ProfileFocusConfig(plateau_min_rounds=1, min_relative_improvement=0.02))
    state = focus.observe(
        focus.initial(),
        round_number=1,
        bottlenecks=(ProfileBottleneck(name="decode", cost=10.0, share=0.5),),
    )
    assert state.active_component == "decode"
    record = RoundRecord(
        round_number=1,
        hypothesis_id="h1",
        commit="b" * 40,
        perf_metric=42.0,
        perf_unit="throughput",
        passed=True,
        reviewed=True,
        official_evaluation=True,
        perf_delta_pct=1.0,
    )
    advanced = focus.record(
        state,
        round_number=1,
        passed=True and record.official_evaluation,
        relative_improvement=(
            record.perf_delta_pct / 100 if record.perf_delta_pct is not None else None
        ),
    )
    assert advanced.active_component is None
