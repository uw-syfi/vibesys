"""Whole dynamic-plugin runs over the production layers with scripted agents."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.loop._harness import (
    PASS,
    AgentTransportError,
    LoopInput,
    ScriptedAgents,
    Turn,
    commit_as_schema_v4,
    edit_to,
    implemented,
    load_state,
    options,
    portfolio,
    run_loop,
    workstream,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.models import WorkstreamPhase
from vs_agent.api import AgentOutputSchemaError
from vs_runtime.api import StructuredResponseError

_SCHEMA_ERRORS = (
    "Output does not match required schema: root: must have required property 'workstreams', "
    "/reasoning: must NOT have more than 2000 characters (got 2762)"
)


def test_a_hypothesis_is_adopted_and_the_next_one_builds_on_it(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    seen: dict[str, int] = {}

    def build_on_first(agent: Turn) -> dict[str, object]:
        seen["second-start"] = agent.value()
        agent.set_value(3)
        agent.evaluate("accuracy")
        return implemented("H2")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2")))
        .implement("H1", edit_to(2, "H1", ("accuracy", "benchmark")))
        .judge("H1", PASS)
        .implement("H2", build_on_first)
        .judge("H2", PASS)
    )

    run = run_loop(loop_input, agents, options(max_rounds=2))

    assert run.error is None
    assert run.succeeded is True
    assert run.notes() == []
    assert agents.unscripted == []
    state = load_state(loop_input, run.run_id)
    first, second = state.workstreams
    assert [item.phase for item in state.workstreams] == [WorkstreamPhase.EVALUATED] * 2
    assert state.baseline is not None
    assert state.baseline.metric_value == 1.0
    assert first.evaluation is not None
    assert first.evaluation.metric_value == 2.0
    # The second hypothesis branches from the first's trusted candidate.
    assert second.parent_revision == first.candidate_revision
    assert seen["second-start"] == 2
    assert second.evaluation is not None
    assert second.evaluation.metric_value == 3.0
    assert state.winner_revision == second.candidate_revision
    assert not state.adoption_pending
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert len(agents.prompts(IMPLEMENTER.id)) == 2


def test_planner_mistakes_are_corrected_and_odd_ids_reach_trusted_rounds(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    odd = ("H1", "KV.Cache_v2 / ../Ünïcode")
    agents = (
        ScriptedAgents()
        # Continues a hypothesis that does not exist; the planner is corrected.
        .plan(
            portfolio(workstream("Q9", continue_hypothesis=True), workstream("Q10")),
            portfolio(*(workstream(identifier) for identifier in odd)),
        )
        .implement(odd[0], edit_to(2, odd[0]))
        .judge(odd[0], PASS)
        .implement(odd[1], edit_to(5, odd[1], ("benchmark",)))
        .judge(odd[1], PASS)
    )

    run = run_loop(loop_input, agents, options(max_in_flight=2))

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    assert run.notes() == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 2
    assert "Correction required" in planner[1]
    assert "'Q9' cannot be continued" in planner[1]
    state = load_state(loop_input, run.run_id)
    assert {item.hypothesis_id for item in state.workstreams} == set(odd)
    assert all(item.phase is WorkstreamPhase.EVALUATED for item in state.workstreams)
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 5\n"


def test_an_underfilled_plan_and_an_update_to_a_failed_hypothesis_do_not_end_the_run(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    abandon = {"hypothesis_id": "A", "disposition": "abandoned", "reason": "The agent died."}
    agents = (
        ScriptedAgents()
        # Two free slots, one workstream: asked once to fill them, the planner
        # keeps its single workstream.
        .plan(portfolio(workstream("A")), portfolio(workstream("A")))
        .implement("A", AgentTransportError("agent CLI exited"))
        # After A exhausted its retries: abandon it and start B.
        .plan(portfolio(workstream("B"), updates=[abandon]))
        .implement("B", edit_to(2, "B"))
        .judge("B", PASS)
    )

    # One attempt per workstream: A fails for good before the next planning call.
    run = run_loop(loop_input, agents, options(max_in_flight=2, max_retries_per_round=1))

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 3
    assert "free slots" in planner[1]
    # The planner sees why A failed, not only that it did.
    assert "agent CLI exited" in planner[2]
    state = load_state(loop_input, run.run_id)
    failed, adopted = state.workstreams
    assert failed.phase is WorkstreamPhase.FAILED
    assert adopted.phase is WorkstreamPhase.EVALUATED
    abandoned = next(item for item in state.search.hypotheses if item.hypothesis_id == "A")
    assert abandoned.strategy == "abandoned"
    assert state.winner_revision == adopted.candidate_revision


def test_repeated_failures_and_a_judge_rejection_are_retried_with_their_feedback(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)

    def keep_failing(agent: Turn) -> dict[str, object]:
        # Three edits that each fail accuracy at the same source line.
        for value in (-1, -2, -3):
            agent.set_value(value)
            agent.evaluate("accuracy")
        return implemented("H1")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")))
        .implement("H1", keep_failing, edit_to(2, "H1"), edit_to(4, "H1"))
        .judge(
            "H1",
            {"passed": False, "analysis": "Unproven.", "feedback": "Show the queue bound holds."},
            PASS,
        )
    )

    run = run_loop(loop_input, agents, options(max_retries_per_round=3))

    assert run.error is None
    assert run.succeeded is True
    assert run.notes() == []
    assert agents.unscripted == []
    first, second, third = agents.prompts(IMPLEMENTER.id, "H1")
    assert "Correction required" not in first
    # The repeated failure ended the attempt without a review or gates.
    assert "3 evaluations in a row failed with the same error" in second
    assert "ValueError" in second
    assert "Show the queue bound holds." in third
    assert len(agents.prompts(JUDGE.id, "H1")) == 2
    state = load_state(loop_input, run.run_id)
    (item,) = state.workstreams
    assert item.phase is WorkstreamPhase.EVALUATED
    assert item.budget.spent == 3
    assert item.evaluation is not None
    assert item.evaluation.metric_value == 4.0


class PlannerCrashError(RuntimeError):
    """The planner's agent process failed in a way the run cannot absorb."""


def test_a_crash_cancels_the_pending_job_and_leaves_state_that_loads(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    os.mkfifo(loop_input.submitted)
    # The input measurement stays queued for the whole run.
    loop_input.hold_jobs()
    jobs: list[str] = []

    def plan_once_the_input_job_is_queued(_agent: Turn) -> dict[str, object]:
        jobs.append(loop_input.submitted.read_text(encoding="utf-8"))
        return portfolio(workstream("H1"))

    def crash(_agent: Turn) -> dict[str, object]:
        (job,) = jobs
        assert f"scancel {job}" not in loop_input.cluster_commands()
        raise PlannerCrashError

    agents = (
        ScriptedAgents()
        .plan(plan_once_the_input_job_is_queued, crash)
        .implement("H1", edit_to(2, "H1"))
        # A rejected candidate is recorded without waiting for the input reading.
        .judge("H1", {"passed": False, "analysis": "Unproven.", "feedback": "Prove it."})
    )

    run = run_loop(loop_input, agents, options(max_rounds=2, max_retries_per_round=1))

    assert isinstance(run.error, PlannerCrashError)
    assert run.succeeded is None
    assert agents.unscripted == []
    (job,) = jobs
    assert f"scancel {job}" in loop_input.cluster_commands()
    state = load_state(loop_input, run.run_id)
    (item,) = state.workstreams
    assert item.phase is WorkstreamPhase.FAILED
    assert [record.round_number for record in state.search.rounds] == [item.sequence]
    assert state.baseline is None


def test_a_crashed_run_resumes_from_older_state_and_finishes(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    first = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), PlannerCrashError("planner died"))
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
    )
    configured = options(max_rounds=2)
    crashed = run_loop(loop_input, first, configured)
    assert crashed.error is not None
    commit_as_schema_v4(loop_input, crashed.run_id)
    seen: dict[str, int] = {}

    def build_on_first(agent: Turn) -> dict[str, object]:
        seen["start"] = agent.value()
        agent.set_value(3)
        return implemented("H2")

    second = (
        ScriptedAgents()
        .plan(portfolio(workstream("H2")))
        .implement("H2", build_on_first)
        .judge("H2", PASS)
    )

    resumed = run_loop(loop_input, second, configured, resume_run_id=crashed.run_id)

    assert resumed.error is None
    assert resumed.succeeded is True
    assert resumed.notes() == []
    assert first.unscripted == second.unscripted == []
    # H1's finished work is not redone.
    assert second.prompts(IMPLEMENTER.id, "H1") == []
    assert seen["start"] == 2
    state = load_state(loop_input, crashed.run_id)
    assert [item.hypothesis_id for item in state.workstreams] == ["H1", "H2"]
    assert state.winner_revision == state.workstreams[1].candidate_revision
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 3\n"


# Hypothesis ids and titles are agent output that becomes workspace names,
# Git refs, state namespaces, and prompt text. Each example is a whole run, so
# the example count stays small; the explicit examples are past failures.
_IDS = st.text(st.characters(exclude_categories=("Cs", "Cc")), min_size=1, max_size=128).filter(
    str.strip
)
_TITLES = st.text(st.characters(exclude_categories=("Cs",)), min_size=1, max_size=80)


@settings(max_examples=3)
@given(identifier=_IDS, title=_TITLES)
@example(identifier="H1", title="Prefix cache")
@example(identifier="KV.Cache_v2 / ../Ünïcode", title="T" * 80)
def test_any_planned_id_and_title_reach_a_trusted_adopted_round(
    identifier: str, title: str
) -> None:
    with tempfile.TemporaryDirectory() as base:
        loop_input = LoopInput.create(Path(base))
        agents = (
            ScriptedAgents()
            .plan(portfolio(workstream(identifier, title=title)))
            .implement(identifier, edit_to(2, identifier))
            .judge(identifier, PASS)
        )

        run = run_loop(loop_input, agents, options())

        assert run.error is None
        assert run.succeeded is True
        assert run.notes() == []
        assert agents.unscripted == []
        (item,) = load_state(loop_input, run.run_id).workstreams
        assert item.hypothesis_id == identifier
        assert item.phase is WorkstreamPhase.EVALUATED
        assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_a_provider_schema_failure_is_corrected_instead_of_ending_the_run(
    tmp_path: Path,
) -> None:
    """Regression: r10's planner exhausted the provider's schema retries and the run ended.

    The planner's first structured turn fails the way the provider reports
    giving up on the schema; it is sent a correction carrying the validation
    errors in the same session, and the run completes.
    """
    loop_input = LoopInput.create(tmp_path)
    agents = (
        ScriptedAgents()
        .plan(AgentOutputSchemaError(_SCHEMA_ERRORS), portfolio(workstream("H1")))
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
    )

    run = run_loop(loop_input, agents, options())

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 2
    assert _SCHEMA_ERRORS in planner[1]
    state = load_state(loop_input, run.run_id)
    assert [item.phase for item in state.workstreams] == [WorkstreamPhase.EVALUATED]


def test_a_planner_that_fails_its_schema_after_correction_ends_the_run_with_the_reason(
    tmp_path: Path,
) -> None:
    """The correction is bounded; then the run fails naming the schema, not a CLI exit."""
    loop_input = LoopInput.create(tmp_path)
    agents = ScriptedAgents().plan(
        AgentOutputSchemaError(_SCHEMA_ERRORS), AgentOutputSchemaError(_SCHEMA_ERRORS)
    )

    run = run_loop(loop_input, agents, options())

    assert isinstance(run.error, StructuredResponseError)
    assert run.error.detail == _SCHEMA_ERRORS
    assert "PortfolioPlan" in str(run.error)
    assert agents.unscripted == []
    assert len(agents.prompts(ORCHESTRATOR.id)) == 2
