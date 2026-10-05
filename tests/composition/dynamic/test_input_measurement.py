"""Input workload failures are permanent facts of the input revision, on the core path."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.composition.dynamic._harness import (
    PASS,
    AgentTransportError,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    edit_to,
    portfolio,
    resume_request,
    run_request,
    workstream,
)

from vibesys.orchestration.dynamic.agents import ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path

MAX_INPUT_ATTEMPTS = 3  # strategy/_config.py: max_input_measurement_attempts
_UNMEASURABLE = "Input not measurable: warmup stopped: 1/100 rounds"


def _failing_input(loop_input: LoopInput) -> None:
    (loop_input.root / "queue.py").write_text("VALUE = 1\nREQUIRED = 100\n", encoding="utf-8")


def _script(agents: ScriptedAgents, first: int, last: int) -> ScriptedAgents:
    for number in range(first, last):
        identifier = f"H{number}"
        agents.plan(portfolio(workstream(identifier)))
        agents.implement(identifier, edit_to(number + 2, identifier)).judge(identifier, PASS)
    return agents


@pytest.mark.parametrize("planning_calls", [1, 2])
def test_permanent_input_failure_is_measured_once_and_told_to_the_planner(
    tmp_path: Path, planning_calls: int
) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    agents = _script(ScriptedAgents(), 0, planning_calls)

    run = run_request(loop_input.request(max_rounds=planning_calls), agents)

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    prompts = agents.prompts(ORCHESTRATOR.id)
    assert len(prompts) == planning_calls
    assert all(_UNMEASURABLE in prompt for prompt in prompts)
    records = CoreRecords(loop_input, run.run_id)
    baseline = records.strategy["baseline"]
    assert baseline["stage"] == "unmeasurable"
    assert baseline["benchmark_passed"] is False
    assert baseline["failure"] == "warmup stopped: 1/100 rounds"
    assert baseline["attempts"] == 1
    # The unmeasurable input has no baseline value, so the best candidate wins.
    selection = records.selection
    assert selection is not None
    assert selection["kind"] == "retained_candidate"
    last = records.strategy["hypotheses"][-1]["rounds"][-1]
    assert selection["revision"] == last["candidate"]
    # The input costs exactly one measurement however long the search runs.
    assert loop_input.sbatch_count() == _candidate_jobs(planning_calls) + _input_jobs()


def _input_jobs() -> int:
    # One job runs the accuracy and benchmark stages of a measurement together.
    return 1


def _candidate_jobs(rounds: int) -> int:
    return rounds


_LEASE_GAP = (
    "gap (owner: host composition and vs-runtime): the host builds its run clock itself "
    "(src/vibesys/run/host.py:572, WallRunClock) and a crashed process keeps its 60 s "
    "lease (src/vibesys/run/core_run.py:39), so resuming in the same minute fails with "
    "'runtime lease unavailable' and a scenario cannot advance time. Needs a clock seam "
    "on LaunchSettings or a lease release when a run ends."
)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_LEASE_GAP)
def test_permanent_input_failure_survives_a_crash_and_resume(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    request = loop_input.request(max_rounds=1)
    first = ScriptedAgents().plan(AgentTransportError("planner died"))
    crashed = run_request(request, first)
    assert crashed.error is not None
    assert loop_input.sbatch_count() == _input_jobs()
    second = (
        ScriptedAgents()
        .plan(portfolio(workstream("H2")))
        .implement("H2", edit_to(3, "H2"))
        .judge("H2", PASS)
    )

    resumed = run_request(resume_request(request, crashed.run_id), second)

    assert resumed.error is None
    assert resumed.succeeded is True
    assert first.unscripted == second.unscripted == []
    # The resumed run measures only the new candidate, never the input again.
    assert loop_input.sbatch_count() == _input_jobs() + _candidate_jobs(1)
    assert _UNMEASURABLE in second.prompts(ORCHESTRATOR.id)[0]
    assert CoreRecords(loop_input, crashed.run_id).strategy["baseline"]["attempts"] == 1


def _interrupt_input_evaluator(loop_input: LoopInput, tmp_path: Path, failures: int) -> Path:
    """Make the benchmark process die before any result for the input's first runs."""
    benchmark = loop_input.root / "benchmark.py"
    original = benchmark.read_text(encoding="utf-8")
    counter = tmp_path / "input-attempts"
    # A missing result is infrastructure evidence, not a workload verdict. Only the
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
        "        raise SystemExit(1)\n" + original,
        encoding="utf-8",
    )
    return counter


_INFRA_GAP = (
    "gap (owner: vs-runtime): a benchmark process that dies without a result record is "
    "recorded as a permanent workload failure, so the input is never re-measured "
    "(attempts stays 1). libs/vs-runtime/src/vs_runtime/_evaluation_jobs.py:263 claims "
    "INFRASTRUCTURE only when a job produced no evidence, but the framed benchmark "
    "always yields evidence; the decoder's BenchmarkFailureKind "
    "(_trusted_evaluation.py:515) is not carried on TrustedEvidence. Candidates share it."
)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_INFRA_GAP)
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
        assert baseline["benchmark_passed"] is None
