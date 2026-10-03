"""Whole dynamic-plugin runs over the production layers with scripted agents."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
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
    planner_history,
    portfolio,
    profile_workstream,
    run_loop,
    workstream,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR, PROFILER
from vibesys.orchestration.dynamic.models import WorkstreamPhase
from vs_agent.api import AgentOutputSchemaError
from vs_runtime.api import StructuredResponseError

if TYPE_CHECKING:
    from collections.abc import Callable

_SCHEMA_ERRORS = (
    "Output does not match required schema: root: must have required property 'workstreams', "
    "/workstreams/0/hypothesis_id: must match pattern"
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
# Git refs, state namespaces, and prompt text. The plan accepts an id only in
# its one canonical spelling (tests/vibesys/orchestration/dynamic/test_plan_ids.py
# covers the rejected ones), so this generates canonical ids. Each example is a
# whole run, so the example count stays small; the explicit examples are past
# failures.
_IDS = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=128,
).filter(lambda value: value == value.strip() and unicodedata.is_normalized("NFC", value))
_TITLES = st.text(st.characters(exclude_categories=("Cs",)), min_size=1, max_size=80)


@settings(max_examples=3)
@given(identifier=_IDS, title=_TITLES)
@example(identifier="H1", title="Prefix cache")
@example(identifier="0", title="0 ")
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


def test_a_plan_that_fails_validation_is_corrected_with_the_field_named_errors(
    tmp_path: Path,
) -> None:
    """The correction names the offending field, as the production client reports it."""
    loop_input = LoopInput.create(tmp_path)
    trailing_space = portfolio(workstream("0 "))
    agents = (
        ScriptedAgents()
        .plan(trailing_space, portfolio(workstream("0")))
        .implement("0", edit_to(2, "0"))
        .judge("0", PASS)
    )

    run = run_loop(loop_input, agents, options())

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 2
    assert "Correction required" in planner[1]
    assert "workstreams.0.implement.hypothesis_id" in planner[1]
    assert "'0 ' is not a valid identifier" in planner[1]
    state = load_state(loop_input, run.run_id)
    assert [item.hypothesis_id for item in state.workstreams] == ["0"]
    assert [item.phase for item in state.workstreams] == [WorkstreamPhase.EVALUATED]


def test_long_agent_text_is_kept_whole_and_the_planner_history_stays_bounded(
    tmp_path: Path,
) -> None:
    """No agent text field is capped, so nothing is cut at the agent boundary.

    Under Codex a capped field is cut off mid-word and the turn still succeeds.
    The planner's history view is bounded where it is rendered instead.
    """
    loop_input = LoopInput.create(tmp_path)
    long = "word " * 4000
    long_workstream = {
        **workstream("H1", title=long, task=long),
        "hypothesis": long,
        "pass_criteria": long,
    }
    plan = {**portfolio(long_workstream), "reasoning": long}

    def long_result(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        return {**implemented("H1"), "summary": long, "next_step": long}

    agents = (
        ScriptedAgents()
        .plan(plan, portfolio(workstream("H2")))
        .implement("H1", long_result)
        .judge("H1", {"passed": True, "analysis": long, "feedback": long})
        .implement("H2", edit_to(3, "H2"))
        .judge("H2", PASS)
    )

    run = run_loop(loop_input, agents, options(max_rounds=2))

    assert run.error is None
    assert agents.unscripted == []
    first = load_state(loop_input, run.run_id).workstreams[0]
    assert first.plan.hypothesis == long
    assert first.plan.task == long
    assert first.plan.pass_criteria == long
    assert first.implementation is not None
    assert first.implementation.summary == long
    assert first.implementation.next_step == long
    assert first.review is not None
    assert first.review.analysis == long
    second_planning = agents.prompts(ORCHESTRATOR.id)[1]
    assert len(second_planning) < 20_000
    assert long not in second_planning


# Accuracy reads ``VALUE`` and passes; the benchmark process exits with a
# failure after measuring, as a benchmark killed at its time limit does.
_SLOW_CANDIDATE = """\
import sys
VALUE = 2
if "--vs-output" in sys.argv:
    raise SystemExit("warmup timed out at 2 requests/s; 80 needed")
"""
# A deadlock guard for turns that wait on each other; each wait ends within
# seconds, and a longer bound never turns a failure into a pass.
_HANDOFF_S = 120.0


def _only_running_evaluation(row: dict[str, object]) -> dict[str, Any]:
    evaluations = row["running_evaluations"]
    assert isinstance(evaluations, list)
    (live,) = evaluations
    assert isinstance(live, dict)
    return live


def test_the_planner_sees_a_running_turns_stage_outcomes_and_its_applied_parks(
    tmp_path: Path,
) -> None:
    """Regression for r13: the planner planned on stale and misreported facts.

    A running implementer turn showed the outcome of the attempt before it, an
    evaluation whose benchmark failed read as an accepted result, and a park
    left no trace in the history.
    """
    loop_input = LoopInput.create(tmp_path)
    second_turn = threading.Event()
    planned = threading.Event()
    seen: dict[str, object] = {}

    def fail_twice(agent: Turn) -> dict[str, object]:
        for value in (-1, -2):
            agent.set_value(value)
            agent.evaluate("accuracy")
        return implemented("A", outcome="blocked")

    def slow_candidate(agent: Turn) -> dict[str, object]:
        (agent.workspace / "queue.py").write_text(_SLOW_CANDIDATE, encoding="utf-8")
        agent.evaluate("accuracy", "benchmark")
        second_turn.set()
        assert planned.wait(_HANDOFF_S)
        return implemented("A", outcome="blocked")

    def finish_while_a_runs(_agent: Turn) -> dict[str, object]:
        assert second_turn.wait(_HANDOFF_S)
        return implemented("B", outcome="blocked")

    def park_running(agent: Turn) -> dict[str, object]:
        seen["operations"] = agent.trusted_operations()
        park = {"hypothesis_id": "A", "disposition": "parked", "reason": "Too slow."}
        return portfolio(workstream("C"), updates=[park])

    def park_finished(_agent: Turn) -> dict[str, object]:
        planned.set()
        park = {"hypothesis_id": "B", "disposition": "parked", "reason": "Blocked."}
        return portfolio(workstream("C"), updates=[park])

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("A"), workstream("B")), park_running, park_finished)
        .plan(portfolio(workstream("D")))
        .implement("A", fail_twice, slow_candidate)
        .implement("B", finish_while_a_runs)
        .implement("C", implemented("C", outcome="blocked"))
        .implement("D", implemented("D", outcome="blocked"))
    )

    run = run_loop(
        loop_input,
        agents,
        options(max_in_flight=2, max_rounds=2, max_repeated_failures=2),
    )

    assert run.error is None
    assert agents.unscripted == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 4
    running = planner_history(planner[1])["A"]
    assert running["phase"] == "implementing"
    # The attempt before the running turn is labeled as such, not as current.
    assert "outcome" not in running
    previous = running["previous_attempt"]
    assert isinstance(previous, dict)
    assert previous["outcome"] == "blocked"
    live = _only_running_evaluation(running)
    assert live["status"] == "failed"
    assert [(stage["kind"], stage["outcome"]) for stage in live["stages"]] == [
        ("accuracy", "passed"),
        ("benchmark", "failed"),
    ]
    assert "warmup timed out at 2 requests/s" in str(live["failure_tail"])
    # The run-wide operations tool says the same: recorded is not passed.
    operations = seen["operations"]
    assert isinstance(operations, dict)
    measured = operations["evaluations"][-1]
    assert measured["evidence_recorded"] is True
    assert [item["outcome"] for item in measured["stage_outcomes"]] == ["passed", "failed"]
    # Parking a running workstream is corrected with the field named.
    assert "hypothesis_updates[0].hypothesis_id: 'A' is still running" in planner[2]
    # The applied park shows in the next planning call's history.
    parked = planner_history(planner[3])["B"]
    assert parked["strategy"] == "parked"
    assert parked["strategy_reason"] == "Blocked."
    state = load_state(loop_input, run.run_id)
    assert [item.hypothesis_id for item in state.workstreams] == ["A", "B", "C", "D"]


def test_a_new_workstream_builds_on_a_named_accuracy_passing_candidate(tmp_path: Path) -> None:
    """Regression for r13: nothing was adopted, so every workstream rebuilt shared work.

    A candidate that passes accuracy but fails its benchmark is not adopted;
    a new workstream that names it as its parent starts from its files. A
    parent that is not a buildable candidate is corrected with the field named.
    """
    loop_input = LoopInput.create(tmp_path)
    seen: dict[str, str] = {}

    def slow_candidate(agent: Turn) -> dict[str, object]:
        (agent.workspace / "queue.py").write_text(_SLOW_CANDIDATE, encoding="utf-8")
        return implemented("A")

    def build_on_a(agent: Turn) -> dict[str, object]:
        seen["start"] = (agent.workspace / "queue.py").read_text(encoding="utf-8")
        return implemented("B", outcome="blocked")

    def child(parent: str) -> dict[str, object]:
        return portfolio({**workstream("B"), "parent_hypothesis_id": parent})

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("A")), child("ghost"), child("A"))
        .implement("A", slow_candidate)
        .judge("A", PASS)
        .implement("B", build_on_a)
    )

    run = run_loop(loop_input, agents, options(max_rounds=2, max_retries_per_round=1))

    assert run.error is None
    assert agents.unscripted == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 3
    assert "Buildable candidates" in planner[1]
    assert "workstreams[0].parent_hypothesis_id: 'ghost' is not a buildable" in planner[2]
    assert seen["start"] == _SLOW_CANDIDATE
    state = load_state(loop_input, run.run_id)
    first, second = state.workstreams
    assert first.evaluation is not None
    assert first.evaluation.accuracy_passed is True
    assert first.evaluation.benchmark_passed is False
    assert second.parent_revision == first.candidate_revision


def test_a_new_workstream_builds_on_content_its_implementer_verified(tmp_path: Path) -> None:
    """Regression for r13: the fastest candidate passed accuracy only in its own evaluations.

    The implementer's submitted evaluation passes accuracy and fails the
    benchmark, then the turn edits past it and stops blocked, so the framework
    never evaluates the candidate. The evaluated revision is still offered,
    and a new workstream naming it starts from exactly the evaluated content.
    """
    loop_input = LoopInput.create(tmp_path)
    seen: dict[str, str] = {}

    def verify_then_break(agent: Turn) -> dict[str, object]:
        (agent.workspace / "queue.py").write_text(_SLOW_CANDIDATE, encoding="utf-8")
        agent.evaluate("accuracy", "benchmark")
        agent.set_value(-5)
        return implemented("A", outcome="blocked")

    def build_on_a(agent: Turn) -> dict[str, object]:
        seen["start"] = (agent.workspace / "queue.py").read_text(encoding="utf-8")
        return implemented("B", outcome="blocked")

    agents = (
        ScriptedAgents()
        .plan(
            portfolio(workstream("A")),
            portfolio({**workstream("B"), "parent_hypothesis_id": "A"}),
        )
        .implement("A", verify_then_break)
        .implement("B", build_on_a)
    )

    run = run_loop(loop_input, agents, options(max_rounds=2, max_retries_per_round=1))

    assert run.error is None
    assert agents.unscripted == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 2
    assert "Buildable candidates" in planner[1]
    assert seen["start"] == _SLOW_CANDIDATE
    state = load_state(loop_input, run.run_id)
    first, second = state.workstreams
    assert first.evaluation is None
    assert first.verified is not None
    assert first.verified.benchmark_passed is False
    assert second.parent_revision == first.verified.revision
    assert second.parent_revision != first.candidate_revision


def _buildable(prompt: str) -> list[dict[str, Any]]:
    """Return the buildable candidates one planning prompt lists, in its order."""
    match = re.search(r"Buildable candidates .*?: (\[[^\n]*\])$", prompt, re.DOTALL | re.MULTILINE)
    assert match is not None
    rows = json.loads(match.group(1))
    assert isinstance(rows, list)
    return rows


def test_failed_benchmarks_reach_the_planner_as_ranked_partial_measurements(
    tmp_path: Path,
) -> None:
    """Regression for r14: every candidate missed the warmup bar by a different margin.

    Their progress reached agents only as prose, so the planner saw
    ``metric_value: null`` for all of them and could not tell the closest one.
    Two candidates fail the benchmark with 14 and 38 of 72 rounds; the
    implementer's own evaluation, the run-wide operations, the history rows,
    and the buildable list all carry the structured measurement, and the
    better candidate is listed first.
    """
    loop_input = LoopInput.create(tmp_path)
    # The input itself misses the bar too, by more than either candidate.
    (loop_input.root / "queue.py").write_text("VALUE = 1\nREQUIRED = 72\n", encoding="utf-8")
    seen: dict[str, dict[str, object]] = {}

    def reach(value: int, identifier: str) -> Callable[[Turn], dict[str, object]]:
        def turn(agent: Turn) -> dict[str, object]:
            (agent.workspace / "queue.py").write_text(
                f"VALUE = {value}\nREQUIRED = 72\n", encoding="utf-8"
            )
            seen[identifier] = agent.evaluate("accuracy", "benchmark")
            return implemented(identifier)

        return turn

    def third_plan(agent: Turn) -> dict[str, object]:
        seen["operations"] = agent.trusted_operations()
        return portfolio(workstream("C"))

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("A")), portfolio(workstream("B")), third_plan)
        .implement("A", reach(14, "A"))
        .judge("A", PASS)
        .implement("B", reach(38, "B"))
        .judge("B", PASS)
        .implement("C", implemented("C", outcome="blocked"))
    )

    run = run_loop(loop_input, agents, options(max_rounds=3, max_retries_per_round=1))

    assert run.error is None
    assert agents.unscripted == []

    def measured(value: int) -> dict[str, object]:
        return {
            "name": "warmup_rounds_per_s",
            "value": float(value),
            "direction": "max",
            "unit": "rounds/s",
            "target": 72.0,
            "progress": {"completed": value, "required": 72, "unit": "rounds"},
        }

    awaited = seen["B"]["result"]
    assert isinstance(awaited, dict)
    assert awaited["outcome"] == "completed"
    benchmark = awaited["stages"][-1]["result"]
    assert benchmark["outcome"] == "failed"
    assert benchmark["partial_measurement"] == measured(38)
    operations = seen["operations"]["evaluations"]
    assert isinstance(operations, list)
    assert [item["stage_outcomes"][-1]["partial_measurement"] for item in operations] == [
        measured(14),
        measured(38),
    ]
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 3
    history = planner_history(planner[2])
    for identifier, value in (("A", 14), ("B", 38)):
        evaluation = history[identifier]["evaluation"]
        assert isinstance(evaluation, dict)
        assert evaluation["benchmark_passed"] is False
        assert evaluation["metric_value"] is None
        assert evaluation["partial_measurement"] == measured(value)
    assert f"before it stopped: {json.dumps(measured(1), separators=(',', ':'))}" in planner[2]
    ranked = _buildable(planner[2])
    assert [row["hypothesis_id"] for row in ranked] == ["B", "A"]
    assert [row["partial_measurement"] for row in ranked] == [measured(38), measured(14)]


def test_a_stopped_warmup_on_slurm_reaches_the_implementer_and_planner_as_its_rate(
    tmp_path: Path,
) -> None:
    """Regression for r19: ten warmups stopped at 7 to 16 tok/s, all with no partial measurement.

    The candidate's benchmark is the bundle's own harness replaying r19's
    recorded warmup, through the production-shaped Slurm config whose benchmark
    arguments carry the port placeholder. The implementer's await reply, the
    planner's operations view, and the next planning prompt each carry the
    achieved rate as a structured measurement, not only the stop's prose.
    """
    loop_input = LoopInput.create(tmp_path, serviced=True)
    seen: dict[str, dict[str, object]] = {}

    def stop_at_warmup(agent: Turn) -> dict[str, object]:
        (agent.workspace / "queue.py").write_text(
            "VALUE = 1\nWARMUP_STOPS = True\n", encoding="utf-8"
        )
        seen["await"] = agent.evaluate("accuracy", "benchmark")
        return implemented("W")

    def second_plan(agent: Turn) -> dict[str, object]:
        seen["operations"] = agent.trusted_operations()
        return portfolio(workstream("X"))

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("W")), second_plan)
        .implement("W", stop_at_warmup)
        .judge("W", PASS)
        .implement("X", implemented("X", outcome="blocked"))
    )

    run = run_loop(loop_input, agents, options(max_rounds=2, max_retries_per_round=1))

    assert run.error is None
    assert agents.unscripted == []
    # r19's eval_eb00846: 1246 output tokens in 176 s, 9 of 72 rounds, against
    # 14343 tokens in 180 s.
    measured = {
        "name": "warmup_output_tokens_per_s",
        "value": pytest.approx(1246 / 176),
        "direction": "max",
        "unit": "output tokens/s",
        "target": pytest.approx(14343 / 180),
        "progress": {"completed": 9, "required": 72, "unit": "rounds"},
    }
    awaited = seen["await"]["result"]
    assert isinstance(awaited, dict)
    benchmark = awaited["stages"][-1]["result"]
    assert benchmark["outcome"] == "failed"
    assert "7.1 output tokens/s achieved" in benchmark["semantic_summary"]
    assert benchmark["partial_measurement"] == measured
    operations = seen["operations"]["evaluations"]
    assert isinstance(operations, list)
    assert [item["stage_outcomes"][-1]["partial_measurement"] for item in operations] == [measured]
    planner = agents.prompts(ORCHESTRATOR.id)
    evaluation = planner_history(planner[1])["W"]["evaluation"]
    assert isinstance(evaluation, dict)
    assert evaluation["benchmark_passed"] is False
    assert evaluation["partial_measurement"] == measured


# The candidate's benchmark blocks reading one byte from a FIFO that the test
# holds open, so its evaluation stays running for as many awaits as the
# implementer chooses, independent of how long each await blocks. Bytes written
# before the benchmark starts wait in the pipe, so the release cannot be lost.
_GATED_CANDIDATE = """\
import os, sys
VALUE = 4
if "--vs-output" in sys.argv:
    os.read(os.open({gate!r}, os.O_RDONLY), 1)
"""
# A short per-call wait: every await before the gate opens returns running.
_SHORT_AWAIT_S = 0.2
_GATED_AWAITS = 3
# One byte per benchmark run the evaluation could make.
_RELEASE = b"x" * 64


def test_an_implementer_await_spans_several_bounds_and_its_turn_completes(
    tmp_path: Path,
) -> None:
    """Regression for r15: a long evaluation outlived the agent's tool-call timeout.

    Each await returns at its bound with the progress recorded so far, and
    the implementer keeps awaiting the same handle until the result arrives.
    """
    loop_input = LoopInput.create(tmp_path)
    gate = tmp_path / "benchmark-gate"
    os.mkfifo(gate)
    # Read-write keeps a writer attached, so the candidate's open never blocks
    # and closing it (even on failure) ends every pending read.
    gate_fd = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
    running: list[dict[str, Any]] = []
    final: dict[str, object] = {}

    def wait_across_bounds(agent: Turn) -> dict[str, object]:
        (agent.workspace / "queue.py").write_text(
            _GATED_CANDIDATE.format(gate=str(gate)), encoding="utf-8"
        )
        handle = agent.submit("accuracy", "benchmark")
        for _ in range(_GATED_AWAITS):
            result = agent.await_once(handle, _SHORT_AWAIT_S)
            assert result["outcome"] == "running"
            running.append(result)
        os.write(gate_fd, _RELEASE)
        while (result := agent.await_once(handle, _SHORT_AWAIT_S))["outcome"] == "running":
            running.append(result)
        final.update(result)
        return implemented("H1")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")))
        .implement("H1", wait_across_bounds)
        .judge("H1", PASS)
    )

    try:
        run = run_loop(loop_input, agents, options(max_rounds=1))
    finally:
        os.close(gate_fd)

    assert run.error is None
    assert agents.unscripted == []
    assert final["outcome"] == "completed"
    assert len(running) >= _GATED_AWAITS
    assert {item["handle_id"] for item in running} == {final["handle_id"]}
    # The service returns the recorded snapshot after an await times out, even
    # if completion races with that snapshot. Only pre-release replies are
    # guaranteed to be nonterminal and to exclude the gated benchmark result.
    before_release = running[:_GATED_AWAITS]
    assert all(item["state"] in {"queued", "starting", "running"} for item in before_release)
    assert all(0 < float(item["next_await_s"]) <= 60 for item in running)
    # The progress names a planned stage and only stages that already finished.
    assert all(item["current_stage"] in {None, "accuracy", "benchmark"} for item in running)
    assert all(
        stage["kind"] == "accuracy" for item in before_release for stage in item["stage_outcomes"]
    )
    state = load_state(loop_input, run.run_id)
    (item,) = state.workstreams
    assert item.phase is WorkstreamPhase.EVALUATED
    assert item.evaluation is not None
    assert item.evaluation.metric_value == 4.0


def _observed_profile(agent: Turn) -> dict[str, object]:
    # An observation cites the trusted capture the host recorded before the turn.
    (recorded,) = agent.accepted_evidence("profile")
    return {
        "outcome": "observed",
        "narrative": "Decode dominates: 75% of the time is in the per-token loop.",
        "evidence_ids": [recorded["evidence_id"]],
        "attribution": [{"name": "decode", "cost": 3.0, "share": 0.75}],
    }


def test_a_planned_profile_reaches_the_next_plan_and_the_trusted_operations(
    tmp_path: Path,
) -> None:
    """The planner can profile a correct candidate and plans on the diagnosis.

    In r14 and r15 candidates kept missing the throughput bar and nothing was
    profiled: only an implementer could reach the profiler, and none did.
    """
    loop_input = LoopInput.create(tmp_path, profiled=True)
    seen: dict[str, object] = {}

    def plan_after_profile(agent: Turn) -> dict[str, object]:
        seen["operations"] = agent.trusted_operations()
        return portfolio(workstream("B"))

    agents = (
        ScriptedAgents()
        .plan(
            portfolio(workstream("A")),
            portfolio(profile_workstream("prof-A", "A", "Where does A spend its time?")),
            plan_after_profile,
        )
        .implement("A", edit_to(2, "A"))
        .judge("A", PASS)
        .profile(_observed_profile)
        .implement("B", implemented("B", outcome="blocked"))
    )

    run = run_loop(loop_input, agents, options(max_rounds=3))

    assert run.error is None
    assert agents.unscripted == []
    state = load_state(loop_input, run.run_id)
    candidate = state.workstreams[0].candidate_revision
    profiler_prompt = agents.prompts(PROFILER.id)[0]
    assert f"Candidate snapshot: `{candidate}`" in profiler_prompt
    assert "Where does A spend its time?" in profiler_prompt
    row = planner_history(agents.prompts(ORCHESTRATOR.id)[2])["prof-A"]
    assert row["status"] == "observed"
    assert row["revision"] == candidate
    assert row["target_hypothesis_id"] == "A"
    assert "Decode dominates" in str(row["diagnosis"])
    assert row["components"] == [{"name": "decode", "share": 0.75}]
    operations = seen["operations"]
    assert isinstance(operations, dict)
    (profiled,) = operations["profiler_operations"]
    assert profiled["principal_id"] == "prof-A"
    assert profiled["state"] == "completed"
    assert profiled["candidate_snapshot_id"] == candidate
    assert profiled["operation_id"] == row["operation_id"]
    # A profile spends a slot of the budget but records no round.
    assert [record.hypothesis_id for record in state.search.rounds] == ["A", "B"]


def test_a_failed_profile_reaches_the_next_plan_as_a_typed_outcome(tmp_path: Path) -> None:
    """A profiler turn that fails is reported to the planner, not replaced by a reply."""
    loop_input = LoopInput.create(tmp_path, profiled=True)
    agents = (
        ScriptedAgents()
        .plan(
            portfolio(profile_workstream("prof-base", None)),
            portfolio(workstream("B")),
        )
        .profile(AgentTransportError("profiler process died"))
        .implement("B", implemented("B", outcome="blocked"))
    )

    run = run_loop(loop_input, agents, options(max_rounds=2))

    assert run.error is None
    assert agents.unscripted == []
    row = planner_history(agents.prompts(ORCHESTRATOR.id)[1])["prof-base"]
    assert row["status"] == "failed"
    assert "profiler process died" in str(row["failure_tail"])
    assert row["diagnosis"] is None
    state = load_state(loop_input, run.run_id)
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.failure is not None


def test_a_profile_of_an_unlisted_target_is_corrected_with_the_field_named(
    tmp_path: Path,
) -> None:
    """A profile target must be a listed buildable candidate; no fallback to the base."""
    loop_input = LoopInput.create(tmp_path, profiled=True)
    agents = (
        ScriptedAgents()
        .plan(
            portfolio(profile_workstream("prof-X", "missing")),
            portfolio(workstream("B")),
        )
        .implement("B", implemented("B", outcome="blocked"))
    )

    run = run_loop(loop_input, agents, options(max_rounds=1))

    assert run.error is None
    assert agents.unscripted == []
    assert agents.prompts(PROFILER.id) == []
    correction = agents.prompts(ORCHESTRATOR.id)[1]
    assert (
        "Correction required: workstreams[0].target_hypothesis_id: 'missing' is not a "
        "buildable candidate"
    ) in correction
    assert load_state(loop_input, run.run_id).profiles == []


def test_a_profile_without_a_provisioned_profiler_is_corrected(tmp_path: Path) -> None:
    """A run without a profiler neither offers nor runs a profile workstream."""
    loop_input = LoopInput.create(tmp_path)
    agents = (
        ScriptedAgents()
        .plan(portfolio(profile_workstream("prof-base", None)), portfolio(workstream("B")))
        .implement("B", implemented("B", outcome="blocked"))
    )

    run = run_loop(loop_input, agents, options(max_rounds=1))

    assert run.error is None
    assert agents.unscripted == []
    first, correction = agents.prompts(ORCHESTRATOR.id)
    assert "profile workstream" not in first
    assert "workstreams[0].kind: this run cannot produce trusted profile evidence" in correction
