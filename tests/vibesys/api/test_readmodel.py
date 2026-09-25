"""The agent-owned read model faithfully projects canonical agent state.

Mirrors the hypothesis/round fixtures in ``tests/server/test_experiments.py``
so these assertions can be checked directly against
``server.api.experiments``'s own projection, which the boundary DTOs must
match field-for-field.
"""

from __future__ import annotations

from typing import Literal, TypedDict, Unpack

from vibesys.agent_run.hypotheses import measurement_delta_reason
from vibesys.agent_run.readmodel import project_run_view as _project_run_view
from vibesys.agent_run.state import (
    AgentRunState,
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisStrategy,
)
from vibesys.api.agent import AgentRunProjection, agent_projection
from vibesys.api.contracts import RunStatus
from vibesys.schemas import CandidateDisposition, derive_hypothesis_title
from vibesys.search.hypothesis import OrchestratorPlan
from vs_loop_state.api import RoundRecord


def _project_agent_view(
    state: AgentRunState,
    *,
    run_id: str,
    status: RunStatus,
    experiment_revision: int,
    loop: str = "multi-agent",
) -> AgentRunProjection:
    view = _project_run_view(
        state,
        run_id=run_id,
        status=status,
        experiment_revision=experiment_revision,
        loop=loop,
    )
    projection = agent_projection(view)
    assert projection is not None
    return projection


class _RoundFields(TypedDict, total=False):
    round_number: int
    commit: str | None
    perf_metric: float | None
    perf_unit: str | None
    passed: bool
    profile_skipped: bool
    hypothesis_id: str | None
    judge_verdict: Literal["pass", "fail", "deferred"] | None
    hypothesis_outcome: str | None
    official_evaluation: bool
    candidate_disposition: str
    perf_delta_pct: float | None


class _HypothesisFields(TypedDict, total=False):
    plan: OrchestratorPlan
    parent_round: int | None
    parent_commit: str | None
    rounds: list[RoundRecord]
    review: HypothesisReview
    resolution: HypothesisResolution | None
    measurement: HypothesisMeasurement | None
    candidate_retained: bool | None
    strategy: HypothesisStrategy
    strategy_reason: str | None
    last_experiment_revision: int


def _round(number: int, **overrides: Unpack[_RoundFields]) -> RoundRecord:
    fields: _RoundFields = {
        "round_number": number,
        "commit": f"c{number}",
        "perf_metric": None,
        "perf_unit": None,
        "passed": False,
    }
    fields.update(overrides)
    return RoundRecord(**fields)


def _hypothesis(
    identifier: str, started_round: int, /, **overrides: Unpack[_HypothesisFields]
) -> Hypothesis:
    fields: _HypothesisFields = {
        "plan": OrchestratorPlan(
            hypothesis_id=identifier,
            hypothesis=f"claim for {identifier}",
            task=f"test {identifier}",
            pass_criteria="",
            reasoning="",
        ),
    }
    fields.update(overrides)
    return Hypothesis(hypothesis_id=identifier, started_round=started_round, **fields)


def _representative_hypothesis() -> Hypothesis:
    return _hypothesis(
        "H-01",
        1,
        rounds=[
            _round(
                1,
                hypothesis_id="H-01",
                perf_metric=100.0,
                perf_unit="ops_s",
                judge_verdict="deferred",
                hypothesis_outcome="proven",
                candidate_disposition="pareto_frontier",
            ),
            _round(2, hypothesis_id="H-01", perf_metric=125.0, perf_unit="ops_s", passed=True),
        ],
        review=HypothesisReview.PASS,
        resolution=HypothesisResolution.DISPROVEN,
        measurement=HypothesisMeasurement(
            round=1,
            metric="throughput",
            value=100.0,
            unit="ops_s",
            direction="max",
            baseline_value=110.0,
            baseline_round=1,
            baseline_commit="base-c1",
            delta_pct=-9.09,
        ),
        candidate_retained=False,
        strategy=HypothesisStrategy.ABANDONED,
        strategy_reason="The official baseline regressed.",
        last_experiment_revision=4,
        parent_commit="parent-sha",
    )


def test_hypothesis_view_matches_fixture_facts_field_for_field() -> None:
    """The boundary projection copies every authoritative fixture fact verbatim."""
    hypothesis = _representative_hypothesis()
    state = AgentRunState(hypotheses=[hypothesis], active_hypothesis_id=None)

    (view,) = _project_agent_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=0
    ).hypotheses

    assert view.hypothesis_id == "H-01"
    # plan.title is unset on this fixture, so the view falls back to a title
    # derived from the claim, matching `derive_hypothesis_title` directly.
    assert view.title == derive_hypothesis_title("claim for H-01")
    assert view.claim == "claim for H-01"
    assert view.action == "test H-01"
    assert (view.first_round, view.last_round) == (1, 2)
    assert view.resolved_outcome == HypothesisResolution.DISPROVEN.value
    assert view.judge_verdict == HypothesisReview.PASS.value
    assert (view.perf_metric, view.perf_unit, view.perf_delta_pct) == (100.0, "ops_s", -9.09)
    assert (view.perf_metric_name, view.perf_direction) == ("throughput", "max")
    assert view.perf_metric_round == 1
    assert (view.perf_baseline_value, view.perf_baseline_round, view.perf_baseline_commit) == (
        110.0,
        1,
        "base-c1",
    )
    assert view.perf_delta_reason == measurement_delta_reason(hypothesis)
    assert view.kept is False
    assert view.strategy_disposition == HypothesisStrategy.ABANDONED.value
    assert view.strategy_reason == "The official baseline regressed."
    assert view.active is False
    assert view.last_experiment_revision == hypothesis.last_experiment_revision == 4
    assert view.parent_commit == hypothesis.parent_commit == "parent-sha"

    # Round-level fields resolve to plain strings rather than the enum
    # members either core-private vocabulary uses (the boundary DTO cannot
    # expose either `HypothesisOutcome` or `HypothesisResolution`; see
    # `HypothesisRoundView`'s docstring).
    api_round = view.rounds[0]
    assert api_round.round_number == 1
    assert api_round.hypothesis_outcome == HypothesisResolution.PROVEN.value
    assert api_round.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    # A round-level verdict is copied verbatim, including "deferred": unlike
    # the hypothesis-level `judge_verdict` above (narrowed to the two
    # terminal values), `HypothesisRoundView.judge_verdict` is typed to carry
    # the third value so a client keeps seeing it unchanged.
    assert api_round.judge_verdict == "deferred"
    assert api_round.perf_metric == 100.0
    assert api_round.reviewed is False


def test_hypothesis_round_drops_a_retired_outcome_rather_than_failing() -> None:
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[
                    _round(
                        1,
                        hypothesis_id="H-01",
                        hypothesis_outcome="retired_value",
                        candidate_disposition="retained",
                    )
                ],
            )
        ]
    )

    (view,) = _project_agent_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=0
    ).hypotheses

    assert view.rounds[0].hypothesis_outcome is None
    assert view.rounds[0].candidate_disposition is None


def test_hypothesis_view_falls_back_to_a_derived_title() -> None:
    hypothesis = _hypothesis("H-01", 1)  # plan.title defaults to "".
    state = AgentRunState(hypotheses=[hypothesis])

    (view,) = _project_agent_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=0
    ).hypotheses

    assert view.title == "claim for H-01"


def test_hypothesis_view_uses_the_orchestrator_title_when_present() -> None:
    hypothesis = _hypothesis(
        "H-01",
        1,
        plan=OrchestratorPlan(
            hypothesis_id="H-01",
            title="Batch decode requests",
            hypothesis="claim for H-01",
            task="test H-01",
            pass_criteria="",
            reasoning="",
        ),
    )
    state = AgentRunState(hypotheses=[hypothesis])

    (view,) = _project_agent_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=0
    ).hypotheses

    assert view.title == "Batch decode requests"


def test_hypothesis_view_reports_active_before_a_round_finishes() -> None:
    state = AgentRunState(
        active_hypothesis_id="H-02",
        hypotheses=[_hypothesis("H-02", 2)],
    )

    (view,) = _project_agent_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=0
    ).hypotheses

    assert view.active is True
    assert view.rounds == []
    assert (view.first_round, view.last_round) == (2, 2)
    assert view.claim == "claim for H-02"


def test_run_view_preserves_hypothesis_append_order_not_first_round_order() -> None:
    """`RunView.hypotheses` is persisted order; `build_experiment_log` re-sorts, this must not."""
    state = AgentRunState(
        hypotheses=[
            _hypothesis("H-02", 5),
            _hypothesis("H-01", 1),
        ]
    )

    run_view = _project_agent_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=0
    )

    assert [view.hypothesis_id for view in run_view.hypotheses] == ["H-02", "H-01"]


def test_run_view_rounds_are_run_wide_and_chronological() -> None:
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[_round(1, hypothesis_id="H-01", perf_metric=100.0, perf_unit="ops_s")],
            ),
            _hypothesis(
                "H-02",
                2,
                rounds=[_round(2, hypothesis_id="H-02")],
            ),
        ]
    )

    run_view = _project_agent_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=7, loop="multi-agent"
    )

    assert [record.round_number for record in run_view.rounds] == [1, 2]
    assert run_view.rounds[0].perf_metric == 100.0
    assert run_view.rounds[1].perf_metric is None
    # An unmeasured round still appears: unlike `performance_rounds`, this is
    # the general-purpose round history, not the performance-plot series.
    assert run_view.current_round == 2
    assert run_view.experiment_revision == 7
    envelope = _project_run_view(
        state, run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=7, loop="multi-agent"
    )
    assert envelope.loop == "multi-agent"
    assert type(envelope.loop) is str
    assert envelope.run_id == "run-1"
    assert envelope.status is RunStatus.UNKNOWN
    assert (
        agent_projection(type(envelope).model_validate_json(envelope.model_dump_json())) == run_view
    )


def test_run_view_current_round_counts_every_completed_round() -> None:
    state = AgentRunState(
        hypotheses=[
            _hypothesis(
                "H-01",
                1,
                rounds=[
                    _round(1, hypothesis_id="H-01"),
                    _round(2, hypothesis_id="H-01"),
                    _round(3, hypothesis_id="H-01"),
                ],
            )
        ]
    )

    run_view = _project_agent_view(
        state, run_id="run-1", status=RunStatus.ACTIVE, experiment_revision=0
    )

    assert run_view.current_round == 3


def test_run_view_reports_no_rounds_or_hypotheses_for_an_empty_state() -> None:
    run_view = _project_agent_view(
        AgentRunState(), run_id="run-1", status=RunStatus.UNKNOWN, experiment_revision=0
    )

    assert run_view.hypotheses == []
    assert run_view.rounds == []
    assert run_view.current_round == 0
    assert run_view.active_hypothesis_id is None
