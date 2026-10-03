"""Input workload failures are permanent facts of the input revision."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.loop._harness import (
    PASS,
    LoopInput,
    ScriptedAgents,
    edit_to,
    load_state,
    options,
    portfolio,
    run_loop,
    workstream,
)
from tests.vibesys.orchestration.dynamic.loop.test_loop import PlannerCrashError

from vibesys.orchestration.dynamic.agents import ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path


def _jobs(loop_input: LoopInput) -> int:
    return sum("sbatch " in command for command in loop_input.cluster_commands())


def _failing_input(loop_input: LoopInput) -> None:
    (loop_input.root / "queue.py").write_text("VALUE = 1\nREQUIRED = 100\n", encoding="utf-8")


@pytest.mark.parametrize("planning_calls", [3, 5])
def test_permanent_input_failure_is_measured_once_and_told_to_the_planner(
    tmp_path: Path, planning_calls: int
) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    agents = ScriptedAgents()
    for number in range(planning_calls):
        identifier = f"H{number}"
        agents.plan(portfolio(workstream(identifier)))
        agents.implement(identifier, edit_to(number + 2, identifier)).judge(identifier, PASS)

    run = run_loop(loop_input, agents, options(max_rounds=planning_calls))

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    # Each candidate has one accuracy job and one benchmark job. The input
    # contributes exactly one additional benchmark, independent of planning.
    assert _jobs(loop_input) == 2 * planning_calls + 1
    prompts = agents.prompts(ORCHESTRATOR.id)
    assert len(prompts) == planning_calls
    assert all("Input not measurable: warmup stopped: 1/100 rounds" in p for p in prompts[1:])
    state = load_state(loop_input, run.run_id)
    assert state.baseline is not None
    assert state.baseline.benchmark_passed is False
    assert state.baseline.benchmark_feedback == "warmup stopped: 1/100 rounds"
    assert state.winner_revision == state.workstreams[-1].candidate_revision


def test_permanent_input_failure_survives_resume(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    configured = options(max_rounds=2)
    first = (
        ScriptedAgents()
        .plan(
            portfolio(workstream("H1")),
            # Two faulted planning turns spend the retry bound and end the run.
            PlannerCrashError("planner died"),
            PlannerCrashError("planner died"),
        )
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
    )
    crashed = run_loop(loop_input, first, configured)
    assert isinstance(crashed.error, PlannerCrashError)
    assert load_state(loop_input, crashed.run_id).winner_revision is None
    before_resume = _jobs(loop_input)
    second = (
        ScriptedAgents()
        .plan(portfolio(workstream("H2")))
        .implement("H2", edit_to(3, "H2"))
        .judge("H2", PASS)
    )

    resumed = run_loop(loop_input, second, configured, resume_run_id=crashed.run_id)

    assert resumed.error is None
    assert resumed.succeeded is True
    assert first.unscripted == second.unscripted == []
    assert _jobs(loop_input) == before_resume + 2
    assert (
        "Input not measurable: warmup stopped: 1/100 rounds" in second.prompts(ORCHESTRATOR.id)[0]
    )


@pytest.mark.parametrize("failures", [1, 5])
def test_transient_input_failure_is_retried_within_its_bound(tmp_path: Path, failures: int) -> None:
    loop_input = LoopInput.create(tmp_path)
    counter = _interrupt_input_evaluator(loop_input, tmp_path, failures)
    agents = ScriptedAgents()
    for number in range(5):
        identifier = f"H{number}"
        agents.plan(portfolio(workstream(identifier)))
        agents.implement(identifier, edit_to(number + 2, identifier)).judge(identifier, PASS)

    run = run_loop(loop_input, agents, options(max_rounds=5))

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    attempts = min(failures + 1, 3)
    assert int(counter.read_text(encoding="utf-8")) == attempts
    assert _jobs(loop_input) == 10 + attempts
    state = load_state(loop_input, run.run_id)
    if failures == 1:
        assert state.baseline is not None
        assert state.baseline.benchmark_passed is True
    else:
        assert state.baseline is None
    assert all("Input not measurable:" not in p for p in agents.prompts(ORCHESTRATOR.id))


def _interrupt_input_evaluator(loop_input: LoopInput, tmp_path: Path, failures: int) -> Path:
    benchmark = loop_input.root / "benchmark.py"
    original = benchmark.read_text(encoding="utf-8")
    counter = tmp_path / "input-attempts"
    # Simulate the evaluator process disappearing before it reports any
    # protocol record. A missing result is infrastructure evidence, not a
    # workload verdict. Only root VALUE=1 fails; candidate jobs are unchanged.
    benchmark.write_text(
        "import pathlib\n"
        "candidate = {}\n"
        'exec(pathlib.Path("queue.py").read_text(), candidate)\n'
        f"counter = pathlib.Path({str(counter)!r})\n"
        'if candidate["VALUE"] == 1:\n'
        "    attempts = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(attempts + 1))\n"
        f"    if attempts < {failures}:\n"
        "        raise SystemExit(1)\n" + original,
        encoding="utf-8",
    )
    return counter


def test_transient_submission_bound_survives_resume(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    counter = _interrupt_input_evaluator(loop_input, tmp_path, 100)
    configured = options(max_rounds=5)
    first = ScriptedAgents()
    for number in range(3):
        identifier = f"H{number}"
        first.plan(portfolio(workstream(identifier)))
        first.implement(identifier, edit_to(number + 2, identifier)).judge(identifier, PASS)
    # Two faulted planning turns spend the retry bound and end the run.
    first.plan(PlannerCrashError("planner died"), PlannerCrashError("planner died"))
    crashed = run_loop(loop_input, first, configured)
    assert isinstance(crashed.error, PlannerCrashError)
    assert int(counter.read_text(encoding="utf-8")) == 3
    assert load_state(loop_input, crashed.run_id).winner_revision is None
    second = ScriptedAgents()
    for number in range(3, 5):
        identifier = f"H{number}"
        second.plan(portfolio(workstream(identifier)))
        second.implement(identifier, edit_to(number + 2, identifier)).judge(identifier, PASS)

    resumed = run_loop(loop_input, second, configured, resume_run_id=crashed.run_id)

    assert resumed.error is None
    assert resumed.succeeded is True
    assert first.unscripted == second.unscripted == []
    assert int(counter.read_text(encoding="utf-8")) == 3
    assert _jobs(loop_input) == 13
