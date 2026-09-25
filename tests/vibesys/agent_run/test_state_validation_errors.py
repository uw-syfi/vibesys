"""Failure-mode tests for the agent-run state aggregate and its transitions."""

import pytest
from pydantic import ValidationError
from tests.support import make_orchestrator_plan

from vibesys.agent_run.hypotheses import (
    append_round,
    apply_strategy_updates,
    project_round_evidence,
    reproject_run_evidence,
    start_hypothesis,
    update_active_hypothesis,
)
from vibesys.agent_run.state import (
    AgentRunState,
    Hypothesis,
    HypothesisStrategy,
    ProfileAttributionSample,
    ProfileGuidanceState,
    ProfileGuidanceStatus,
    ProfileGuidedComponent,
    ProfileImprovementSample,
)
from vibesys.schemas import HypothesisStrategyUpdate, OrchestratorPlan
from vs_loop_state.api import RoundRecord


def _plan(identifier: str) -> OrchestratorPlan:
    return make_orchestrator_plan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        task=f"implement {identifier}",
        criteria="tests pass",
        reasoning="test the claim",
    )


def _round(number: int, hypothesis_id: str) -> RoundRecord:
    return RoundRecord(
        round_number=number,
        commit=f"{number:040x}",
        perf_metric=None,
        perf_unit=None,
        passed=True,
        hypothesis_id=hypothesis_id,
    )


def test_history_rounds_must_be_unique_and_ordered() -> None:
    samples = [
        ProfileAttributionSample(round=2, cost=1.0, share=0.5),
        ProfileAttributionSample(round=1, cost=1.0, share=0.5),
    ]
    with pytest.raises(ValidationError, match="attribution history rounds must be unique"):
        ProfileGuidedComponent(name="attn", attribution_history=samples)
    improvements = [
        ProfileImprovementSample(round=1, relative_improvement=0.1),
        ProfileImprovementSample(round=1, relative_improvement=0.2),
    ]
    with pytest.raises(ValidationError, match="improvement history rounds must be unique"):
        ProfileGuidedComponent(name="attn", improvement_history=improvements)


def test_profile_guidance_cursor_must_name_the_single_active_component() -> None:
    open_a = ProfileGuidedComponent(name="a")
    open_b = ProfileGuidedComponent(name="b")
    with pytest.raises(ValidationError, match="component names must be unique"):
        ProfileGuidanceState(components=[open_a, open_a.model_copy()])
    active = ProfileGuidedComponent(name="a", status=ProfileGuidanceStatus.ACTIVE)
    with pytest.raises(ValidationError, match="only active component"):
        ProfileGuidanceState(components=[active, open_b])
    state = ProfileGuidanceState(active_component="a", components=[active, open_b])
    assert state.active_component == "a"


def test_hypothesis_identity_and_round_ownership_are_validated() -> None:
    with pytest.raises(ValidationError, match="plan hypothesis_id must match"):
        Hypothesis(hypothesis_id="H-1", plan=_plan("H-2"), started_round=1)
    unordered = [_round(2, "H-1"), _round(1, "H-1")]
    with pytest.raises(ValidationError, match="rounds must be unique and ordered"):
        Hypothesis(hypothesis_id="H-1", plan=_plan("H-1"), started_round=1, rounds=unordered)
    with pytest.raises(ValidationError, match="round hypothesis_id must match its owning"):
        Hypothesis(
            hypothesis_id="H-1",
            plan=_plan("H-1"),
            started_round=1,
            rounds=[_round(1, "H-2")],
        )


def test_active_hypothesis_must_be_known_and_strategically_available() -> None:
    parked = Hypothesis(
        hypothesis_id="H-1",
        plan=_plan("H-1"),
        started_round=1,
        strategy=HypothesisStrategy.PARKED,
    )
    with pytest.raises(ValidationError, match="must name a known hypothesis"):
        AgentRunState(active_hypothesis_id="nope", hypotheses=[parked])
    with pytest.raises(ValidationError, match="strategically available"):
        AgentRunState(active_hypothesis_id="H-1", hypotheses=[parked])


def test_start_hypothesis_rejects_active_blank_and_duplicate_ids() -> None:
    started = start_hypothesis(AgentRunState(), _plan("H-1"), started_round=1)
    with pytest.raises(ValueError, match="another is active"):
        start_hypothesis(started, _plan("H-2"), started_round=2)
    idle = AgentRunState(hypotheses=started.hypotheses)
    with pytest.raises(ValueError, match="hypothesis ID must not be blank"):
        start_hypothesis(
            idle, _plan("H-2").model_copy(update={"hypothesis_id": "  "}), started_round=2
        )
    with pytest.raises(ValueError, match="'H-1' already exists"):
        start_hypothesis(idle, _plan(" H-1 "), started_round=2)


def test_update_active_hypothesis_requires_the_active_id() -> None:
    started = start_hypothesis(AgentRunState(), _plan("H-1"), started_round=1)
    other = Hypothesis(hypothesis_id="H-2", plan=_plan("H-2"), started_round=1)
    with pytest.raises(ValueError, match="must preserve the active hypothesis ID"):
        update_active_hypothesis(started, other)
    with pytest.raises(ValueError, match="when none is active"):
        update_active_hypothesis(AgentRunState(), other)
    checkpoint = started.active_hypothesis
    assert checkpoint is not None
    checkpoint.feedback = "keep going"
    updated = update_active_hypothesis(started, checkpoint)
    assert updated.active_hypothesis is not None
    assert updated.active_hypothesis.feedback == "keep going"


def test_append_round_rejects_missing_mismatched_and_duplicate_rounds() -> None:
    with pytest.raises(ValueError, match="no hypothesis is active"):
        append_round(AgentRunState(), _round(1, "H-1"), keep_active=False)
    started = start_hypothesis(AgentRunState(), _plan("H-1"), started_round=1)
    with pytest.raises(ValueError, match="must match the active hypothesis"):
        append_round(started, _round(1, "H-2"), keep_active=False)
    kept = append_round(started, _round(1, "H-1"), keep_active=True)
    assert kept.active_hypothesis_id == "H-1"
    with pytest.raises(ValueError, match="round 1 already exists"):
        append_round(kept, _round(1, "H-1"), keep_active=True)


def test_project_round_evidence_rejects_foreign_and_duplicate_rounds() -> None:
    hypothesis = Hypothesis(
        hypothesis_id="H-1",
        plan=_plan("H-1"),
        started_round=1,
        rounds=[_round(1, "H-1")],
    )
    with pytest.raises(ValueError, match="must match its owning hypothesis"):
        project_round_evidence(
            hypothesis, _round(2, "H-2"), prior_rounds=[], space=AgentRunState().metrics
        )
    with pytest.raises(ValueError, match="round 1 already belongs to hypothesis"):
        project_round_evidence(
            hypothesis, _round(1, "H-1"), prior_rounds=[], space=AgentRunState().metrics
        )


def test_reprojection_rejects_a_round_owned_by_an_unknown_hypothesis() -> None:
    state = start_hypothesis(AgentRunState(), _plan("H-1"), started_round=1)
    # Appending in place bypasses field validation, modelling a corrupted checkpoint.
    state.hypotheses[0].rounds.append(_round(1, "ghost"))
    with pytest.raises(ValueError, match="round 1 names unknown hypothesis 'ghost'"):
        reproject_run_evidence(state)


def test_strategy_updates_reject_duplicate_hypothesis_ids() -> None:
    started = start_hypothesis(AgentRunState(), _plan("H-1"), started_round=1)
    completed = append_round(started, _round(1, "H-1"), keep_active=False)
    update = HypothesisStrategyUpdate(hypothesis_id="H-1", disposition="parked", reason="later")
    with pytest.raises(ValueError, match="duplicate strategy update for hypothesis 'H-1'"):
        apply_strategy_updates(completed, [update, update])
