"""Pure tests for the agent-loop state model."""

import math

import pytest
from pydantic import ValidationError

from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.state.api import RoundRecord
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.hypothesis.state import Hypothesis, HypothesisState


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="h1",
        task="optimize the queue",
        pass_criteria="the checker passes",  # noqa: S106  # LW-040066 [S106]; the argument is a fixture literal, not a credential.
        reasoning="reduce contention",
    )


def test_agent_run_state_round_trips_through_its_external_schema() -> None:
    hypothesis = Hypothesis(
        hypothesis_id="h1",
        plan=_plan(),
        started_round=3,
        parent_round=2,
        parent_commit="a" * 40,
        gate_approved_metrics={"throughput": 42.0},
    )

    state = HypothesisState(active_hypothesis_id="h1", hypotheses=[hypothesis])
    loaded = HypothesisState.model_validate_json(state.model_dump_json(), strict=True)

    assert loaded == state
    assert loaded.schema_version == 1


def test_hypothesis_rejects_unknown_external_fields() -> None:
    state = Hypothesis(hypothesis_id="h1", plan=_plan(), started_round=1)
    payload = state.model_dump(mode="json") | {"unexpected": True}

    with pytest.raises(ValidationError, match="unexpected"):
        Hypothesis.model_validate(payload, strict=True)


def test_hypothesis_rejects_coercion_and_non_finite_metrics() -> None:
    with pytest.raises(ValidationError, match="started_round"):
        Hypothesis.model_validate(
            {"hypothesis_id": "h1", "plan": _plan(), "started_round": "1"},
            strict=True,
        )

    with pytest.raises(ValidationError, match="gate_approved_metrics"):
        Hypothesis(
            hypothesis_id="h1",
            plan=_plan(),
            started_round=1,
            gate_approved_metrics={"throughput": math.inf},
        )


def test_hypothesis_clone_has_independent_nested_state() -> None:
    state = Hypothesis(
        hypothesis_id="h1",
        plan=_plan(),
        started_round=1,
        gate_approved_metrics={"throughput": 1.0},
    )

    cloned = state.clone()
    cloned.gate_approved_metrics["throughput"] = 2.0

    assert state.gate_approved_metrics == {"throughput": 1.0}
    assert cloned.gate_approved_metrics == {"throughput": 2.0}


def test_state_written_before_the_metric_space_field_loads_as_the_empty_strict_space() -> None:
    """State from a run predating ``MetricSpace`` persistence still loads."""
    legacy = HypothesisState.model_validate({"schema_version": 1, "hypotheses": []})
    assert legacy.metrics == MetricSpace()


def test_state_rejects_duplicate_hypothesis_ids() -> None:
    one = Hypothesis(hypothesis_id="h1", plan=_plan(), started_round=1)
    with pytest.raises(ValidationError, match="hypothesis IDs must be unique"):
        HypothesisState(hypotheses=[one, one.clone()])


def test_state_rejects_a_dangling_active_pointer() -> None:
    with pytest.raises(ValidationError, match="active_hypothesis_id"):
        HypothesisState(active_hypothesis_id="missing")


def _plan_for(identifier: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=identifier,
        task="optimize the queue",
        pass_criteria="the checker passes",  # noqa: S106  # LW-040067 [S106]; the argument is a fixture literal, not a credential.
        reasoning="reduce contention",
    )


def test_state_rejects_duplicate_round_numbers_across_hypotheses() -> None:
    one = Hypothesis(
        hypothesis_id="h1",
        plan=_plan_for("h1"),
        started_round=1,
        rounds=[
            RoundRecord(
                round_number=1,
                commit="a" * 40,
                perf_metric=None,
                perf_unit=None,
                hypothesis_id="h1",
                passed=True,
            )
        ],
    )
    two = Hypothesis(
        hypothesis_id="h2",
        plan=_plan_for("h2"),
        started_round=1,
        rounds=[
            RoundRecord(
                round_number=1,
                commit="b" * 40,
                perf_metric=None,
                perf_unit=None,
                hypothesis_id="h2",
                passed=True,
            )
        ],
    )
    with pytest.raises(ValidationError, match="globally unique"):
        HypothesisState(hypotheses=[one, two])
