"""Input workload failures are permanent facts of the input revision, on the core path."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.composition.dynamic._harness import (
    PASS,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    Turn,
    edit_to,
    implemented,
    portfolio,
    resume_request,
    run_request,
    simulated_clock,
    workstream,
)
from tests.support.host_clock import HostCrashedError

from vibesys.api import RunStatus
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path

# A benchmark killed with no result record is ambiguous: measured once more, then final.
MAX_INPUT_ATTEMPTS = 2
_UNMEASURABLE = "Input not measurable: warmup stopped: 1/100 rounds"


def _failing_input(loop_input: LoopInput) -> None:
    (loop_input.root / "queue.py").write_text("VALUE = 1\nREQUIRED = 100\n", encoding="utf-8")


def _script(agents: ScriptedAgents, first: int, last: int) -> ScriptedAgents:
    for number in range(first, last):
        identifier = f"H{number}"
        agents.plan(portfolio(workstream(identifier)))
        agents.implement(identifier, edit_to(number + 2, identifier)).judge(identifier, PASS)
    return agents


def _input_jobs() -> int:
    # One job runs the accuracy and benchmark stages of a measurement together.
    return 1


def _candidate_jobs(rounds: int) -> int:
    return rounds


def test_permanent_input_failure_survives_a_crash_and_resume(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    request = loop_input.request(max_rounds=2)
    clock = simulated_clock()

    def host_dies(_agent: Turn) -> dict[str, object]:
        # The host dies once this planning turn is committed, before round 2 starts.
        clock.crash_on_next_clock_call()
        return portfolio()

    first = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), host_dies)
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
    )
    crashed = run_request(request, first, clock=clock)
    assert isinstance(crashed.error, HostCrashedError)
    assert loop_input.sbatch_count() == _input_jobs() + _candidate_jobs(1)
    started_from: list[int] = []

    def build_on_first(agent: Turn) -> dict[str, object]:
        started_from.append(agent.value())
        agent.set_value(3)
        return implemented("H2")

    second = (
        ScriptedAgents()
        .plan(portfolio(workstream("H2", parent_hypothesis_id="H1")))
        .implement("H2", build_on_first)
        .judge("H2", PASS)
    )

    resumed = run_request(resume_request(request, crashed.run_id), second, clock=clock)

    resumed.raise_error()
    assert resumed.error is None
    assert resumed.succeeded is True
    assert first.unscripted == second.unscripted == []
    # The resumed run measures only the new candidate, never the input again.
    assert loop_input.sbatch_count() == _input_jobs() + _candidate_jobs(2)
    # Both planning calls, before and after the crash, are told the input is unmeasurable.
    assert _UNMEASURABLE in first.prompts(ORCHESTRATOR.id)[0]
    assert _UNMEASURABLE in second.prompts(ORCHESTRATOR.id)[0]
    records = CoreRecords(loop_input, crashed.run_id)
    baseline = records.strategy["baseline"]
    assert baseline["stage"] == "unmeasurable"
    assert baseline["benchmark_passed"] is False
    assert baseline["failure"] == "warmup stopped: 1/100 rounds"
    assert baseline["attempts"] == 1
    # The unmeasurable input has no baseline value, so the best candidate wins.
    assert resumed.status is RunStatus.COMPLETED
    assert records.run["result"]["reason"].startswith("adopted: ")
    selection = records.selection
    assert selection is not None
    assert selection["kind"] == "retained_candidate"
    assert selection["revision"] == records.strategy["hypotheses"][-1]["rounds"][-1]["candidate"]
    # H1's finished work is not redone, and H2 builds on it.
    assert second.prompts(IMPLEMENTER.id, "H1") == []
    assert started_from == [2]
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 3\n"


def _interrupt_input_evaluator(loop_input: LoopInput, tmp_path: Path, failures: int) -> Path:
    """Make the benchmark process die before any result for the input's first runs."""
    benchmark = loop_input.root / "benchmark.py"
    original = benchmark.read_text(encoding="utf-8")
    counter = tmp_path / "input-attempts"
    # A missing result is ambiguous evidence, not a workload verdict. Only the
    # input (VALUE == 1) fails; candidate jobs are unchanged.
    benchmark.write_text(
        "import pathlib\n"
        "candidate = {}\n"
        'exec(pathlib.Path("queue.py").read_text(), candidate)\n'
        f"counter = pathlib.Path({str(counter)!r})\n"
        'if candidate["VALUE"] == 1:\n'
        "    attempts = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(attempts + 1))\n"
        f"    if attempts < {failures}:\n"
        '        raise SystemExit("benchmark server killed: out of memory")\n' + original,
        encoding="utf-8",
    )
    return counter


@pytest.mark.parametrize("failures", [1, 5])
def test_transient_input_failure_is_retried_within_its_bound(tmp_path: Path, failures: int) -> None:
    loop_input = LoopInput.create(tmp_path)
    counter = _interrupt_input_evaluator(loop_input, tmp_path, failures)
    agents = _script(ScriptedAgents(), 0, 1)

    run = run_request(loop_input.request(max_rounds=1), agents)

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    attempts = min(failures + 1, MAX_INPUT_ATTEMPTS)
    assert int(counter.read_text(encoding="utf-8")) == attempts
    assert loop_input.sbatch_count() == attempts + _candidate_jobs(1)
    baseline = CoreRecords(loop_input, run.run_id).strategy["baseline"]
    assert baseline["attempts"] == attempts
    if failures == 1:
        assert baseline["stage"] == "measured"
        assert baseline["benchmark_passed"] is True
    else:
        assert baseline["stage"] == "unmeasurable"
        # The second killed attempt is final: a failed benchmark, not a missing one.
        assert baseline["benchmark_passed"] is False
        # The agents are told why the input was given up, not only that it was.
        assert "out of memory" in baseline["failure"]
