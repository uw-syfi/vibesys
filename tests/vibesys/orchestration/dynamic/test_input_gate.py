"""Measuring the input and gating candidates against it."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    EvaluationTransportError,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    input_calls,
    portfolio,
    throughput,
    two_epoch_script,
)

from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicState,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import (
    AgentCapability,
    BenchmarkEvaluation,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


def test_candidate_that_does_not_beat_the_input_is_never_adopted(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("slower"), portfolio("faster")],
            IMPLEMENTER.id: [implementation("slower"), implementation("faster")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )

    async def scenario() -> tuple[FakeRun, DynamicState | None, DynamicState | None]:
        run = baseline_run(tmp_path, script)
        # Input 20, then a regression to 12, then a real improvement to 25.
        run.evaluation.script_benchmark(throughput(20.0), throughput(12.0), throughput(25.0))
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=1, max_in_flight=1))
        after_regression = await run.state.load(DynamicState)
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1))
        return run, after_regression, await run.state.load(DynamicState)

    run, after_regression, state = asyncio.run(scenario())

    assert after_regression is not None
    assert after_regression.baseline is not None
    assert after_regression.baseline.metrics == {"throughput": 20.0}
    assert after_regression.winner_revision is None
    assert [record.candidate_disposition for record in after_regression.search.rounds] == [
        "discard"
    ]
    planner_messages = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    assert '"throughput":20.0' in planner_messages[1]
    assert state is not None
    assert len(run.evaluation.benchmark_calls) == 3
    faster = next(item for item in state.workstreams if item.hypothesis_id == "faster")
    assert state.winner_revision == faster.candidate_revision


class _InputBenchmarkFailsOnceError(RuntimeError):
    """Synthetic evaluator transport failure on the first input measurement."""


def test_failed_input_measurement_is_retried_before_the_next_epoch(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("first"), portfolio("second")],
            IMPLEMENTER.id: [implementation("first"), implementation("second")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )

    async def scenario() -> tuple[FakeRun, DynamicState | None]:
        fake = baseline_run(tmp_path, script)
        # The first input measurement fails in transport; epoch 1 candidate 12
        # is recorded ungated; the input then measures 20.
        fake.evaluation.script_benchmark(
            _InputBenchmarkFailsOnceError(),
            throughput(12.0),
            throughput(20.0),
            throughput(15.0),
        )
        status = await PLUGIN.orchestrate(fake, dynamic_options(max_rounds=2, max_in_flight=1))
        assert status is RunStatus.SUCCEEDED
        return fake, await fake.state.load(DynamicState)

    fake, state = asyncio.run(scenario())

    assert state is not None
    assert state.baseline is not None
    assert state.baseline.metrics == {"throughput": 20.0}
    assert any("baseline measurement failed" in call.message for call in fake.observations.calls)
    # The epoch-1 candidate (12) was recorded before the input was measured;
    # selection still rejects it, and the epoch-2 candidate (15) is gated.
    assert state.winner_revision is None


def test_input_that_fails_the_benchmark_twice_is_recorded_and_gates_nothing(
    tmp_path: Path,
) -> None:
    """A benchmark that ran and rejected the input twice is a property of the input.

    Re-measuring it every epoch costs a cluster job and delays each epoch;
    candidates then need only a passing trusted benchmark, and the planner is
    told why the input failed so the first candidate can satisfy the benchmark.
    """
    rejection = "prefix-cache preflight failed: server reported no prefix-cache hit"
    script = two_epoch_script()

    async def scenario() -> tuple[FakeRun, DynamicState | None]:
        fake = baseline_run(tmp_path, script)
        fake.evaluation.script_root_benchmark(
            BenchmarkEvaluation(executed=True, feedback=rejection),
            BenchmarkEvaluation(executed=True, feedback=rejection),
        )
        fake.evaluation.script_benchmark(throughput(12.0), throughput(15.0))
        run = fake
        assert await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1)) is (
            RunStatus.SUCCEEDED
        )
        return fake, await fake.state.load(DynamicState)

    fake, state = asyncio.run(scenario())
    assert input_calls(fake) == 2
    assert state is not None
    assert state.baseline is not None
    assert state.baseline.benchmark_passed is False
    plans = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    # The first plan does not wait for the input measurement; later ones see it.
    assert all("fails the trusted benchmark" in plan and rejection in plan for plan in plans[1:])
    second = next(item for item in state.workstreams if item.hypothesis_id == "second")
    assert state.winner_revision == second.candidate_revision


def test_input_benchmark_that_did_not_run_is_measured_again(tmp_path: Path) -> None:
    """A benchmark that never ran says nothing about the input; it is retried."""
    script = two_epoch_script()

    async def scenario() -> tuple[FakeRun, DynamicState | None]:
        run = baseline_run(tmp_path, script)
        run.evaluation.script_benchmark(
            BenchmarkEvaluation(executed=False, feedback="Slurm job failed to start"),
            throughput(12.0),
            throughput(10.0),
            throughput(15.0),
        )
        assert await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1)) is (
            RunStatus.SUCCEEDED
        )
        return run, await run.state.load(DynamicState)

    run, state = asyncio.run(scenario())
    assert len(run.evaluation.benchmark_calls) == 4
    assert state is not None
    assert state.baseline is not None
    assert state.baseline.metrics == {"throughput": 10.0}


def test_input_measurement_runs_beside_the_first_workstream_and_still_gates_it(
    tmp_path: Path,
) -> None:
    """Planning does not wait for the input benchmark; candidate decisions do.

    The input measurement costs a cluster job before the first planner turn.
    It only gates which candidates may be kept, so the first workstream starts
    while it runs, and a slower candidate is still discarded against it.
    """
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("slower")],
            IMPLEMENTER.id: [implementation("slower")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id:
            held_input.release()
        return script.respond(role, history, message, response)

    run = FakeRun(
        PLUGIN,
        project_root=tmp_path,
        facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
        responder=respond,
        supported_extra_tools={"evaluation", "profiler"},
        supports_parallel_candidates=True,
        supported_agent_capabilities={
            AgentCapability.MCP_SERVERS,
            AgentCapability.SESSION_REUSE,
            AgentCapability.PROVIDER_SESSION_RESUME,
        },
    )
    run.evaluation.script_benchmark(INPUT_BASELINE, throughput(0.5))
    # The input benchmark is held until an implementer turn releases it, so a
    # policy that waits for the input before planning never finishes.
    held_input = run.evaluation.gate("benchmark", 0)

    async def scenario() -> DynamicState | None:
        options = dynamic_options(max_rounds=1, max_in_flight=1, official_eval_every=1)
        assert await PLUGIN.orchestrate(run, options) is RunStatus.SUCCEEDED
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())
    assert held_input.released
    assert held_input.finished
    assert state is not None
    assert state.baseline is not None
    assert state.baseline.metric_value == 1.0
    assert state.winner_revision is None


def test_resume_without_a_planning_call_still_gates_on_the_input(tmp_path: Path) -> None:
    """A resumed run that only recovers work measures the input before adopting.

    The stop lands while the last budgeted workstream runs and before the input
    has a reading, so the resume never plans; the recovered candidate (12) must
    still be compared with the input (20) and not adopted.
    """
    orchestrating: asyncio.Future[RunStatus] | None = None
    implementer_calls = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal implementer_calls
        if role.id == ORCHESTRATOR.id:
            return portfolio("slower")
        if role.id == IMPLEMENTER.id:
            implementer_calls += 1
            if implementer_calls == 1:
                assert orchestrating is not None
                orchestrating.cancel()
            return implementation("slower")
        return {"passed": True, "analysis": "Candidate is correct."}

    async def scenario() -> tuple[FakeRun, DynamicState | None]:
        nonlocal orchestrating
        fake = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        fake.evaluation.script_root_benchmark(EvaluationTransportError(), throughput(20.0))
        fake.evaluation.script_benchmark(throughput(12.0))
        run = fake
        options = dynamic_options(max_rounds=1, max_in_flight=1)
        orchestrating = asyncio.ensure_future(PLUGIN.orchestrate(run, options))
        with pytest.raises(asyncio.CancelledError):
            await orchestrating
        assert await PLUGIN.orchestrate(run, options) is RunStatus.SUCCEEDED
        return fake, await fake.state.load(DynamicState)

    fake, state = asyncio.run(scenario())

    assert len([s for s in fake.agents.sessions if s.role.id == ORCHESTRATOR.id]) == 1
    assert input_calls(fake) >= 1
    assert state is not None
    assert state.baseline is not None
    assert state.baseline.metrics == {"throughput": 20.0}
    assert [record.candidate_disposition for record in state.search.rounds] == ["discard"]
    assert state.winner_revision is None


def test_input_benchmark_failure_that_ran_is_measured_again_before_it_is_recorded(
    tmp_path: Path,
) -> None:
    """One executed failure of the input may be contention with agent work.

    Recording it at once would disable the input gate for the run; a second
    measurement that passes gates the candidate (12) against the input (20).
    """
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("slower")],
            IMPLEMENTER.id: [implementation("slower")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> tuple[FakeRun, DynamicState | None]:
        fake = baseline_run(tmp_path, script)
        fake.evaluation.script_root_benchmark(
            BenchmarkEvaluation(executed=True, feedback="server start timed out"), throughput(20.0)
        )
        fake.evaluation.script_benchmark(throughput(12.0))
        run = fake
        assert await PLUGIN.orchestrate(run, dynamic_options(max_rounds=1, max_in_flight=1)) is (
            RunStatus.SUCCEEDED
        )
        return fake, await fake.state.load(DynamicState)

    fake, state = asyncio.run(scenario())

    assert input_calls(fake) == 2
    assert state is not None
    assert state.baseline is not None
    assert state.baseline.benchmark_passed is True
    assert state.baseline.metrics == {"throughput": 20.0}
    assert state.winner_revision is None


@pytest.mark.parametrize("planning_calls", [3, 5])
def test_input_that_failed_the_benchmark_is_never_measured_again_in_a_run(
    tmp_path: Path, planning_calls: int
) -> None:
    """An executed input failure costs one re-measurement, whatever follows.

    The input is measured once, measured once more after its first executed
    failure, and recorded. Neither later planning calls nor later candidate
    decisions benchmark it again (each is a cluster job).
    """
    names = [f"w{number}" for number in range(planning_calls)]
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio(name) for name in names],
            IMPLEMENTER.id: [implementation(name) for name in names],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}] * planning_calls,
        }
    )

    async def scenario() -> tuple[FakeRun, DynamicState | None]:
        run = baseline_run(tmp_path, script)
        run.evaluation.script_root_benchmark(
            BenchmarkEvaluation(executed=True, feedback="preflight failed"),
            BenchmarkEvaluation(executed=True, feedback="preflight failed"),
        )
        run.evaluation.script_benchmark(*(throughput(10.0 + n) for n in range(planning_calls)))
        status = await PLUGIN.orchestrate(
            run, dynamic_options(max_rounds=planning_calls, max_in_flight=1)
        )
        assert status is RunStatus.SUCCEEDED
        return run, await run.state.load(DynamicState)

    run, state = asyncio.run(scenario())

    assert len([role for role, _, _ in script.calls if role == ORCHESTRATOR.id]) == planning_calls
    assert input_calls(run) == 2
    assert state is not None
    assert state.baseline is not None
    assert state.baseline.benchmark_passed is False
