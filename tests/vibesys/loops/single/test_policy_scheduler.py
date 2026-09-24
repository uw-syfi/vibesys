"""Single strategy terminal decisions over completed round evidence."""

from __future__ import annotations

from vibesys.agent_run.attempts import AttemptState
from vibesys.agent_run.options import AgentOrchestrationOptions
from vibesys.agent_run.state import AgentRunState
from vibesys.loops.single.hypothesis import HypothesisEngine
from vibesys.loops.single.session import (
    AttemptRequest,
    RoundSelection,
    SingleRound,
    SingleSession,
)
from vibesys.schemas import OrchestratorPlan
from vs_loop_state.api import RoundRecord


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="h1",
        hypothesis="cache decode",
        task="implement cache",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="measure decode",
    )


def _session() -> tuple[SingleSession, SingleRound]:
    plan = _plan()
    engine = HypothesisEngine.create(AgentRunState()).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    session = SingleSession.__new__(SingleSession)
    session.engine = engine
    session.options = AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=3,
        max_retries_per_round=3,
        judge_every=1,
        official_eval_every=2,
        memory_layout="files",
    )
    selection = RoundSelection(engine.state, hypothesis, plan, None)
    request = AttemptRequest(1, plan, None, [], hypothesis, "latency")
    attempt = AttemptState(agent_run_state=engine.state, feedback=None)
    return session, SingleRound(selection, request, attempt)


def test_review_failure_retains_bounded_claim_and_sets_exhaustion_carry() -> None:
    session, selected = _session()
    selected.attempt.feedback = "fix validation"
    record = RoundRecord(
        round_number=1,
        commit=None,
        perf_metric=None,
        perf_unit=None,
        hypothesis_id="h1",
        passed=False,
        judge_verdict="fail",
        hypothesis_outcome="rejected",
    )

    engine, carry, exhaustion = session.complete_policy_round(selected, record)

    active = engine.state.active_hypothesis
    assert active is not None
    assert active.feedback == "fix validation"
    assert active.continuation_rounds == 1
    assert exhaustion == "fix validation"
    assert "fix validation" in (carry.exhaustion_info or "")


def test_terminal_success_releases_claim_and_reports_discarded_official_candidate() -> None:
    session, selected = _session()
    selected.attempt.passed = True
    record = RoundRecord(
        round_number=1,
        commit=None,
        hypothesis_id="h1",
        passed=True,
        judge_verdict="pass",
        official_evaluation=True,
        candidate_retained=False,
        perf_metric=12.0,
        perf_unit="ops",
    )

    engine, carry, exhaustion = session.complete_policy_round(selected, record)

    assert engine.state.active_hypothesis is None
    assert exhaustion is None
    assert "not retained: 12.0 ops" in (carry.regression_info or "")
