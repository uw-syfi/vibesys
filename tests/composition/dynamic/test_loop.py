"""Whole dynamic runs on the core path, over the production host and scripted agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.composition.dynamic._harness import (
    PASS,
    AgentTransportError,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    Turn,
    edit_to,
    implemented,
    portfolio,
    run_request,
    workstream,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_agent.api import AgentOutputSchemaError
from vs_runtime.api.core import RunStalledError

if TYPE_CHECKING:
    from pathlib import Path

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
        return implemented("H2")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2", parent_hypothesis_id="H1")))
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
        .implement("H2", build_on_first)
        .judge("H2", PASS)
    )

    run = run_request(loop_input.request(max_rounds=2), agents)

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    first, second = records.strategy["hypotheses"]
    assert [item["hypothesis_id"] for item in (first, second)] == ["H1", "H2"]
    (first_round,) = first["rounds"]
    (second_round,) = second["rounds"]
    assert records.baseline_metric() == 1.0
    assert first_round["metrics"][0]["value"] == 2.0
    assert second_round["metrics"][0]["value"] == 3.0
    # The second hypothesis branches from the first's trusted candidate.
    assert records.attempt("H2")["parent"] == first_round["candidate"]
    assert seen["second-start"] == 2
    # The search keeps the best trusted candidate.
    selection = records.selection
    assert selection is not None
    assert selection["kind"] == "retained_candidate"
    assert selection["revision"] == second_round["candidate"]
    assert len(agents.prompts(IMPLEMENTER.id)) == 2
    assert loop_input.sbatch_count() == 3
    # Adoption applies the winner to the input project.
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 3\n"


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
        .implement(odd[1], edit_to(5, odd[1]))
        .judge(odd[1], PASS)
    )

    run = run_request(loop_input.request(max_in_flight=2), agents)

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    assert run.notes() == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 2
    assert "Correction required" in planner[1]
    assert "'Q9' cannot be continued" in planner[1]
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    assert {item["hypothesis_id"] for item in records.strategy["hypotheses"]} == set(odd)
    assert all(item["rounds"][0]["eligible"] for item in records.strategy["hypotheses"])
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 5\n"


def test_a_judge_rejection_is_retried_with_its_feedback(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")))
        .implement("H1", edit_to(2, "H1"), edit_to(4, "H1"))
        .judge(
            "H1",
            {"passed": False, "analysis": "Unproven.", "feedback": "Show the queue bound holds."},
            PASS,
        )
    )

    run = run_request(loop_input.request(max_retries_per_round=3), agents)

    assert run.error is None
    assert run.succeeded is True
    assert run.notes() == []
    assert agents.unscripted == []
    first, second = agents.prompts(IMPLEMENTER.id, "H1")
    assert "Feedback from the prior attempt" not in first
    assert "Show the queue bound holds." in second
    assert len(agents.prompts(JUDGE.id, "H1")) == 2
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    (item,) = records.strategy["hypotheses"]
    assert item["rounds"][-1]["metrics"][0]["value"] == 4.0
    assert records.selection is not None
    assert records.selection["revision"] == item["rounds"][-1]["candidate"]
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 4\n"


def test_an_ambiguous_dispatched_turn_stalls_the_run_without_replanning(tmp_path: Path) -> None:
    """A transport loss after dispatch leaves provider acceptance unknown.

    The run must neither finish nor replace the turn's work: A is never abandoned or
    given another turn, and the run ends with a typed stall over a committed record that
    has not ended, so a resume inspects the dispatch first.
    """
    loop_input = LoopInput.create(tmp_path)
    abandon = {
        "hypothesis_id": "A",
        "disposition": "abandoned",
        "reason_kind": "lower_priority",
        "reason": "The agent died.",
    }
    agents = (
        ScriptedAgents()
        # Two free slots, one workstream: asked once to fill them, the planner keeps its
        # single workstream.
        .plan(portfolio(workstream("A")), portfolio(workstream("A")))
        .implement("A", AgentTransportError("agent CLI exited"))
        # Abandoning A after ambiguous provider acceptance is rejected, and so is its
        # correction; once corrections run out only the valid workstream B is scheduled.
        .plan(*[portfolio(workstream("B"), updates=[abandon])] * 3)
        .implement("B", edit_to(2, "B"))
        .judge("B", PASS)
    )

    run = run_request(loop_input.request(max_in_flight=2, max_retries_per_round=1), agents)

    assert isinstance(run.error, RunStalledError)
    assert "dispatch_turn" in str(run.error)
    assert run.succeeded is None
    assert agents.unscripted == []
    assert len(agents.prompts(IMPLEMENTER.id, "A")) == 1
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("running", None)
    attempt = records.attempt("A")
    assert attempt["phase"] == "implement"
    assert not attempt["withdrawn"]
    rounds = {item["hypothesis_id"]: item["rounds"] for item in records.strategy["hypotheses"]}
    assert rounds["A"] == []
    assert records.selection is None


# The planner's turn fails the way the provider reports giving up on its schema. Core's
# turn result carries only a failed status, so the strategy corrects without the text.
_ERROR_TEXT_GAP = (
    "TurnResult has no failure detail: the correction cannot carry the provider's "
    "validation errors, libs/vs-core/src/vs_core/types/sessions.py:378 (TurnResult) and "
    "src/vibesys/orchestration/dynamic/strategy/_planner.py:130 (_parse); owner vs-core"
)
_PLANNER_FAULT_GAP = (
    "the planner is corrected max_corrections times and then the run fails; the legacy loop "
    "asked a fresh planning turn within max_retries_per_round, "
    "src/vibesys/orchestration/dynamic/strategy/_planner.py:199 (on_turn); owner strategy"
)


@pytest.mark.parametrize(
    "schema_failures",
    [
        1,
        pytest.param(
            2, marks=pytest.mark.xfail(strict=True, reason=_PLANNER_FAULT_GAP), id="2-xfail"
        ),
    ],
)
def test_a_provider_schema_failure_is_corrected_instead_of_ending_the_run(
    tmp_path: Path, schema_failures: int
) -> None:
    """Regression: r10's planner exhausted the provider's schema retries and the run ended."""
    loop_input = LoopInput.create(tmp_path)
    failures = [AgentOutputSchemaError(_SCHEMA_ERRORS)] * schema_failures
    agents = (
        ScriptedAgents()
        .plan(*failures, portfolio(workstream("H1")))
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
    )

    run = run_request(loop_input.request(), agents)

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    assert len(agents.prompts(ORCHESTRATOR.id)) == schema_failures + 1
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")


@pytest.mark.xfail(strict=True, reason=_ERROR_TEXT_GAP)
def test_a_correction_names_the_errors_the_provider_reported(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    agents = (
        ScriptedAgents()
        .plan(AgentOutputSchemaError(_SCHEMA_ERRORS), portfolio(workstream("H1")))
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
    )

    run = run_request(loop_input.request(), agents)

    assert run.succeeded is True
    assert _SCHEMA_ERRORS in agents.prompts(ORCHESTRATOR.id)[1]


def test_a_planner_that_fails_its_schema_after_correction_ends_the_run(tmp_path: Path) -> None:
    """Correction and turn retries are bounded; then the run ends without success."""
    loop_input = LoopInput.create(tmp_path)
    agents = ScriptedAgents().plan(*[AgentOutputSchemaError(_SCHEMA_ERRORS)] * 8)

    run = run_request(loop_input.request(), agents)

    assert run.succeeded is not True
    assert agents.unscripted == []
    assert 2 <= len(agents.prompts(ORCHESTRATOR.id)) <= 8
    records = CoreRecords(loop_input, run.run_id)
    status, outcome = records.outcome
    assert status == "terminal"
    assert outcome != "success"
    assert records.strategy["hypotheses"] == []


def test_a_plan_the_schema_rejects_is_corrected_and_the_run_completes(tmp_path: Path) -> None:
    """An identifier with trailing space fails the planner's schema; the next plan is accepted."""
    loop_input = LoopInput.create(tmp_path)
    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("0 ")), portfolio(workstream("0")))
        .implement("0", edit_to(2, "0"))
        .judge("0", PASS)
    )

    run = run_request(loop_input.request(), agents)

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    assert len(agents.prompts(ORCHESTRATOR.id)) == 2
    records = CoreRecords(loop_input, run.run_id)
    assert [item["hypothesis_id"] for item in records.strategy["hypotheses"]] == ["0"]


@pytest.mark.xfail(strict=True, reason=_ERROR_TEXT_GAP)
def test_a_plan_that_fails_validation_is_corrected_with_the_field_named_errors(
    tmp_path: Path,
) -> None:
    """The correction names the offending field, as the production client reports it."""
    loop_input = LoopInput.create(tmp_path)
    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("0 ")), portfolio(workstream("0")))
        .implement("0", edit_to(2, "0"))
        .judge("0", PASS)
    )

    run = run_request(loop_input.request(), agents)

    assert run.succeeded is True
    planner = agents.prompts(ORCHESTRATOR.id)
    assert "Correction required" in planner[1]
    assert "workstreams.0.implement.hypothesis_id" in planner[1]
    assert "'0 ' is not a valid identifier" in planner[1]
