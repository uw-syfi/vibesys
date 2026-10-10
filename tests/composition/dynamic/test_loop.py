"""Whole dynamic runs on the core path, over the production host and scripted agents.

A run costs seconds, almost all of it in the evaluations (every one is a few dozen
connector processes and a Git worktree), not in the agent turns. A scenario therefore
packs every behavior that shares a precondition into one run: the planner is asked
again and again inside one run, and parallel workstreams exercise different implementer
and judge faults side by side. What a run measures is paid once, however many behaviors
it shows.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
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
    simulated_clock,
    workstream,
)
from tests.support.host_clock import HostCrashedError

from vibesys.api import RunFailureKind, RunStatus
from vibesys.events import CoreEventType
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_agent.api import AgentOutputSchemaError, AgentSpawnError, AgentTurnTimeoutError
from vs_project.api import Project
from vs_runtime.api.core import RunStalledError

if TYPE_CHECKING:
    from collections.abc import Callable

_LOOP_EXAMPLES = 20 if os.environ.get("VIBESYS_FULL_PROPERTIES") == "1" else 1
_SCHEMA_ERRORS = (
    "Output does not match required schema: root: must have required property 'workstreams', "
    "/workstreams/0/hypothesis_id: must match pattern"
)
_DROP_BUDGET = 2
"""``DynamicConfig.max_turn_drops``: how often one logical turn is asked again."""
_LOST = "agent CLI exited"


def _assert_long_text_kept_whole_yet_history_bounded(
    records: CoreRecords, agents: ScriptedAgents, long: str
) -> None:
    attempt = records.attempt("H1")
    for field in ("hypothesis", "task", "pass_criteria"):
        assert attempt["plan"][field] == long
    assert attempt["summary"] == long
    assert attempt["next_step"] == long
    second_prompt = agents.prompts(ORCHESTRATOR.id)[1]
    assert len(second_prompt) < 20_000
    assert long not in second_prompt


def test_a_hypothesis_is_adopted_and_the_next_one_builds_on_it(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    seen: dict[str, int] = {}
    # No agent text field is capped, so nothing is cut at the agent boundary.
    long = "word " * 4000
    long_workstream = {
        **workstream("H1", title=long, task=long),
        "hypothesis": long,
        "pass_criteria": long,
    }

    def build_on_first(agent: Turn) -> dict[str, object]:
        seen["second-start"] = agent.value()
        agent.set_value(3)
        return implemented("H2")

    def long_result(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        return {**implemented("H1"), "summary": long, "next_step": long}

    def start_from_default(agent: Turn) -> dict[str, object]:
        seen["third-start"] = agent.value()
        agent.set_value(2)
        return implemented("H3")

    agents = (
        ScriptedAgents()
        .plan(
            {**portfolio(long_workstream), "reasoning": long},
            portfolio(workstream("H2", parent_hypothesis_id="H1")),
            portfolio(workstream("H3")),
        )
        .implement("H1", long_result)
        .judge("H1", {"passed": True, "analysis": long, "feedback": long})
        .implement("H2", build_on_first)
        .judge("H2", PASS)
        .implement("H3", start_from_default)
        .judge("H3", PASS)
    )

    run = run_request(loop_input.request(max_rounds=3), agents, slurm_process=loop_input.connector)

    assert run.error is None
    assert (run.succeeded, run.status) == (True, RunStatus.COMPLETED)
    assert run.result is not None
    assert run.result.failure is None
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    assert records.run["result"]["reason"].startswith("adopted: ")
    first, second, third = records.strategy["hypotheses"]
    assert [item["hypothesis_id"] for item in (first, second, third)] == ["H1", "H2", "H3"]
    (first_round,) = first["rounds"]
    (second_round,) = second["rounds"]
    assert records.baseline_metric() == 1.0
    assert first_round["metrics"][0]["value"] == 2.0
    assert second_round["metrics"][0]["value"] == 3.0
    # The second hypothesis branches from the first's trusted candidate.
    assert records.attempt("H2")["parent"] == first_round["candidate"]
    assert seen["second-start"] == 2
    _assert_long_text_kept_whole_yet_history_bounded(records, agents, long)
    # A workstream that names no parent starts from the input, not from the best trusted
    # candidate (VALUE = 1 here, while H1 and H2 retained 2 and 3); the prompt says so, and
    # the best candidate is reachable only as an explicitly named parent.
    assert seen["third-start"] == 1
    assert records.attempt("H3")["parent"] != first_round["candidate"]
    third_prompt = agents.prompts(ORCHESTRATOR.id)[2]
    assert "root or the best trusted candidate" not in third_prompt
    assert "a new workstream that names no parent starts from it" in third_prompt
    assert "Buildable candidates" in third_prompt
    # The search keeps the best trusted candidate.
    selection = records.selection
    assert selection is not None
    assert selection["kind"] == "retained_candidate"
    assert selection["revision"] == second_round["candidate"]
    assert len(agents.prompts(IMPLEMENTER.id)) == 3
    assert loop_input.sbatch_count() == 4
    # Adoption applies the winner to the input project.
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 3\n"


def test_every_kind_of_planner_mistake_is_corrected_in_one_run(tmp_path: Path) -> None:
    """The planner is asked again after each mistake, and the run ends with its last plan.

    In order: two lost turns (a transport drop within the budget), a provider schema
    failure, a plan whose id the schema rejects, a plan that continues a hypothesis that
    does not exist, then a plan of odd identifiers and a very long title that reaches
    trusted rounds.
    """
    loop_input = LoopInput.create(tmp_path)
    odd = ("0", "KV.Cache_v2 / ../Ünïcode")
    lost = AgentTransportError(_LOST)
    agents = (
        ScriptedAgents()
        .plan(
            lost,
            lost,
            AgentOutputSchemaError(_SCHEMA_ERRORS),
            portfolio(workstream("0 ")),
            portfolio(workstream("Q9", continue_hypothesis=True), workstream("Q10")),
            portfolio(workstream(odd[0], title="T" * 80), workstream(odd[1])),
        )
        .implement(odd[0], edit_to(2, odd[0]))
        .judge(odd[0], PASS)
        .implement(odd[1], edit_to(5, odd[1]))
        .judge(odd[1], PASS)
    )

    run = run_request(
        loop_input.request(max_in_flight=2), agents, slurm_process=loop_input.connector
    )

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    assert run.notes() == []
    planner = agents.prompts(ORCHESTRATOR.id)
    assert len(planner) == 2 + 1 + 1 + 1 + 1
    # A lost turn is asked again as it was; each mistake is corrected by naming it.
    assert all("Correction required" not in prompt for prompt in planner[:3])
    assert _SCHEMA_ERRORS in planner[3]
    assert "Correction required" in planner[4]
    assert "workstreams.0.implement.hypothesis_id" in planner[4]
    assert "'0 ' is not a valid identifier" in planner[4]
    assert "Correction required" in planner[5]
    assert "'Q9' cannot be continued" in planner[5]
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    assert {item["hypothesis_id"] for item in records.strategy["hypotheses"]} == set(odd)
    assert all(item["rounds"][0]["eligible"] for item in records.strategy["hypotheses"])
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 5\n"


def test_implementer_and_judge_faults_in_parallel_workstreams_leave_the_others_unchanged(
    tmp_path: Path,
) -> None:
    """Three workstreams run side by side, each meeting a different fault.

    A's implementer turn is lost beyond the budget, so its item fails and the run goes on.
    B's implementer and judge turns are lost within the budget, then its judge rejects the
    first change and the retry is accepted: the retry continues the implementer's provider
    conversation and workspace and carries the judge's feedback. C is unaffected.
    """
    loop_input = LoopInput.create(tmp_path)
    lost = AgentTransportError(_LOST)
    rejection = {
        "passed": False,
        "analysis": "Unproven.",
        "feedback": "Show the queue bound holds.",
    }
    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("A"), workstream("B"), workstream("C")))
        .implement("A", *[lost] * (_DROP_BUDGET + 1))
        .implement("B", *[lost] * _DROP_BUDGET, edit_to(2, "B"), edit_to(4, "B"))
        .judge("B", *[lost] * _DROP_BUDGET, rejection, PASS)
        .implement("C", edit_to(3, "C"))
        .judge("C", PASS)
    )

    run = run_request(
        loop_input.request(max_in_flight=3, max_retries_per_round=3),
        agents,
        slurm_process=loop_input.connector,
    )

    assert run.error is None
    assert run.succeeded is True
    assert run.notes() == []
    assert agents.unscripted == []
    assert len(agents.prompts(IMPLEMENTER.id, "A")) == _DROP_BUDGET + 1
    implementer = agents.prompts(IMPLEMENTER.id, "B")
    assert len(implementer) == _DROP_BUDGET + 2
    assert "Feedback from the prior attempt" not in implementer[_DROP_BUDGET]
    assert "Show the queue bound holds." in implementer[-1]
    assert len(agents.prompts(JUDGE.id, "B")) == _DROP_BUDGET + 2
    one, two = agents.invocations(IMPLEMENTER.id, "B")[-2:]
    assert one.session_key == two.session_key
    assert one.workspace == two.workspace
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    rounds = {item["hypothesis_id"]: item["rounds"] for item in records.strategy["hypotheses"]}
    (lost_round,) = rounds["A"]
    assert not lost_round["eligible"]
    assert "lost" in lost_round["failure"]
    assert rounds["B"][-1]["metrics"][0]["value"] == 4.0
    assert rounds["C"][0]["eligible"]
    selection = records.selection
    assert selection is not None
    assert selection["revision"] == rounds["B"][-1]["candidate"]
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 4\n"


@pytest.mark.parametrize(
    "failures",
    [
        (AgentSpawnError("claude", "exit status 1"), AgentTurnTimeoutError(30), OSError("pipe")),
        (
            subprocess.CalledProcessError(1, ["claude"]),
            RuntimeError("claude exited with code 1"),
            AgentTransportError(_LOST),
        ),
    ],
    ids=["spawn-timeout-broken-pipe", "exit-status-runtime-transport"],
)
def test_a_planner_turn_killed_by_any_cli_failure_ends_the_run_without_a_stall(
    tmp_path: Path, failures: tuple[Exception, ...]
) -> None:
    """Each failure is a lost turn that is asked again, until the budget runs out."""
    assert len(failures) == _DROP_BUDGET + 1
    loop_input = LoopInput.create(tmp_path)
    agents = ScriptedAgents().plan(*failures)

    run = run_request(loop_input.request(), agents, slurm_process=loop_input.connector)

    assert not isinstance(run.error, RunStalledError), run.error
    assert run.succeeded is not True
    assert agents.unscripted == []
    assert len(agents.prompts(ORCHESTRATOR.id)) == _DROP_BUDGET + 1
    status, outcome = CoreRecords(loop_input, run.run_id).outcome
    assert status == "terminal"
    assert outcome != "success"


def test_a_planner_that_fails_its_schema_after_correction_ends_the_run(tmp_path: Path) -> None:
    """Correction and turn retries are bounded; then the run ends without success."""
    loop_input = LoopInput.create(tmp_path)
    agents = ScriptedAgents().plan(*[AgentOutputSchemaError(_SCHEMA_ERRORS)] * 8)

    run = run_request(loop_input.request(), agents, slurm_process=loop_input.connector)

    assert run.succeeded is not True
    assert agents.unscripted == []
    assert 2 <= len(agents.prompts(ORCHESTRATOR.id)) <= 8
    records = CoreRecords(loop_input, run.run_id)
    status, outcome = records.outcome
    assert status == "terminal"
    assert outcome != "success"
    assert records.strategy["hypotheses"] == []


def test_failed_benchmarks_reach_the_planner_as_ranked_partial_measurements(
    tmp_path: Path,
) -> None:
    """Regression for r14: candidates that miss the warmup bar differ by how far they got.

    Two candidates fail the benchmark with 14 and 38 of 72 rounds. The record keeps the
    structured measurement of each, and the next plan lists the closer candidate first.
    """
    loop_input = LoopInput.create(tmp_path)
    # The input itself misses the bar too, by more than either candidate.
    (loop_input.root / "queue.py").write_text("VALUE = 1\nREQUIRED = 72\n", encoding="utf-8")

    def reach(value: int, identifier: str) -> Callable[[Turn], dict[str, object]]:
        def turn(agent: Turn) -> dict[str, object]:
            agent.write_queue(f"VALUE = {value}\nREQUIRED = 72\n")
            return implemented(identifier)

        return turn

    later = ["C", "D"]

    def refill(agent: Turn) -> dict[str, object]:
        """Fill exactly the free slots the prompt offers from the workstreams not yet planned.

        A and B run in parallel threads, so they may finish one after the other (two
        refills of one slot each) or together (one refill of two slots). Each order is
        a legitimate run, so the script answers the slots it is asked for.
        """
        taken = [later.pop(0) for _ in range(min(agent.slots, len(later)))]
        return portfolio(*(workstream(name) for name in taken))

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("A"), workstream("B")), refill, refill)
        .implement("A", reach(14, "A"))
        .judge("A", PASS)
        .implement("B", reach(38, "B"))
        .judge("B", PASS)
        .implement("C", implemented("C", outcome="blocked"))
        .implement("D", implemented("D", outcome="blocked"))
    )

    run = run_request(
        loop_input.request(max_rounds=2, max_in_flight=2),
        agents,
        slurm_process=loop_input.connector,
    )

    assert agents.unscripted == []
    assert run.error is None
    records = CoreRecords(loop_input, run.run_id)

    def measured(value: int) -> dict[str, object]:
        return {
            "name": "warmup_rounds_per_s",
            "value": float(value),
            "direction": "max",
            "unit": "rounds/s",
            "target": 72.0,
            "completed": float(value),
            "required": 72.0,
            "progress_unit": "rounds",
        }

    rounds = {item["hypothesis_id"]: item["rounds"][0] for item in records.strategy["hypotheses"]}
    for identifier, value in (("A", 14), ("B", 38)):
        assert rounds[identifier]["benchmark_passed"] is False
        assert rounds[identifier]["metrics"] == []
        assert rounds[identifier]["partial"] == measured(value)
    planner = agents.prompts(ORCHESTRATOR.id)
    # One planning call per refill, none beyond the two scripted and none to correct one.
    assert 2 <= len(planner) <= 3
    assert later == []
    assert sorted(rounds) == ["A", "B", "C", "D"]
    # The last plan is the first that sees both finished candidates, whichever order they
    # finished in.
    buildable = planner[-1].split("Buildable candidates")[1]
    order = re.findall(r"hypothesis `([A-Z])`", buildable)
    assert order[:2] == ["B", "A"]
    assert "partial warmup_rounds_per_s = 38.0 rounds/s" in buildable
    assert re.search(r"progress 38(\.0)? of 72(\.0)? rounds", buildable)
    # No input and no candidate is trusted: the run fails, with a typed reason and counts.
    assert (run.succeeded, run.status) == (False, RunStatus.FAILED)
    assert records.strategy["baseline"]["stage"] == "unmeasurable"
    assert records.outcome == ("terminal", "failure")
    assert records.run["result"]["reason"].startswith("no trusted result")
    assert "warmup stopped: 1/72 rounds" in records.run["result"]["reason"]
    assert records.selection is None
    assert run.result is not None
    failure = run.result.failure
    assert failure is not None
    assert failure.kind is RunFailureKind.BUDGET_EXHAUSTED
    assert failure.reason.startswith("no trusted result")
    assert failure.workstreams_started == len(rounds)
    assert failure.candidates_kept == 0
    published = [event.data for event in run.events if event.type is CoreEventType.RUN_FAILED]
    assert [getattr(data, "failure", None) for data in published] == [failure]


# Hypothesis ids and titles are agent output that becomes workspace names, Git refs,
# state namespaces, core identities, and prompt text. The plan accepts an id only in
# its one canonical spelling, so this generates canonical ids. Each example is a whole
# run, so a pull request draws one generated example; the pinned past failures (id "0",
# a title of 80 characters, a path-like Unicode id) run in every planner-mistake run
# above. The scheduled workflow sets ``VIBESYS_FULL_PROPERTIES=1`` and draws many more.
_IDS = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=128,
).filter(lambda value: value == value.strip() and unicodedata.is_normalized("NFC", value))
_TITLES = st.text(st.characters(exclude_categories=("Cs",)), min_size=1, max_size=80)


@settings(max_examples=_LOOP_EXAMPLES)
@given(identifier=_IDS, title=_TITLES)
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

        run = run_request(loop_input.request(), agents, slurm_process=loop_input.connector)

        assert run.error is None
        assert run.succeeded is True
        assert agents.unscripted == []
        (item,) = CoreRecords(loop_input, run.run_id).strategy["hypotheses"]
        assert item["hypothesis_id"] == identifier
        assert item["rounds"][0]["eligible"]


def test_a_lease_that_cannot_be_released_does_not_replace_the_runs_own_error(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    clock = simulated_clock()
    run_ids: list[str] = []

    def unreadable_lease() -> None:
        state = Project.open(loop_input.root).state.state_store_namespace(run_ids[0])
        (state.external_directory() / "store.json").write_text("{not json", encoding="utf-8")

    def host_dies(_agent: Turn) -> dict[str, object]:
        clock.crash_on_next_clock_call(aftermath=unreadable_lease)
        return portfolio()

    agents = ScriptedAgents().plan(host_dies)

    crashed = run_request(
        loop_input.request(max_rounds=1),
        agents,
        clock=clock,
        on_handle=lambda handle: run_ids.append(handle.run_id),
        slurm_process=loop_input.connector,
    )

    # The release in the run's ``finally`` fails to read the lease document; the run's
    # own failure is the one reported.
    assert isinstance(crashed.error, HostCrashedError)
