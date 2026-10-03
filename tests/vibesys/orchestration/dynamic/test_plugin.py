"""Public behavior of dynamic portfolio orchestration."""

from __future__ import annotations

import asyncio
import tempfile
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from vibesys.orchestration.dynamic import (
    PLUGIN,
    REGISTRATION,
    DynamicOptions,
    DynamicState,
    PortfolioPlan,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR, PROFILER
from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentCapability,
    BenchmarkEvaluation,
    BenchmarkObjective,
    LocalValidationEvaluation,
    MetricDirection,
    Run,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeEvaluation, FakeRun

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole, Workspace


def _options(**changes: object) -> DynamicOptions:
    return DynamicOptions.model_validate(
        {
            "interface": "service",
            "max_rounds": 1,
            "max_retries_per_round": 1,
            "judge_every": 3,
            "official_eval_every": 2,
            "max_in_flight": 2,
            "metric_space": {
                "objectives": [{"name": "throughput", "direction": "max"}],
            },
            **changes,
        }
    )


def _portfolio(
    *identifiers: str,
    continue_hypothesis: bool = False,
    request_evaluation: bool = True,
) -> dict[str, object]:
    return {
        "reasoning": "Explore independent limiting mechanisms.",
        "workstreams": [
            {
                "hypothesis_id": identifier,
                "title": f"Investigate {identifier}",
                "hypothesis": f"Mechanism {identifier} limits the objective.",
                "task": f"Implement and verify {identifier}.",
                "pass_criteria": "The change is correct and measurably improves the objective.",
                "request_evaluation": request_evaluation,
                "continue_hypothesis": continue_hypothesis,
            }
            for identifier in identifiers
        ],
    }


def _implementation(identifier: str) -> dict[str, object]:
    return {
        "summary": f"Implemented {identifier}.",
        "outcome": "nominated",
        "evidence": [{"location": f"evidence/{identifier}.json", "purpose": "local verification"}],
    }


class _Script:
    def __init__(self, replies: dict[str, list[object]]) -> None:
        self._replies = {role: deque(values) for role, values in replies.items()}
        self.calls: list[tuple[str, str | None, str]] = []

    def respond(
        self,
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, None, message))
        reply = self._replies[role.id].popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _JudgeTransportError(RuntimeError):
    """Synthetic agent transport failure for policy recovery tests."""


class _EvaluationTransportError(RuntimeError):
    """Synthetic trusted-evaluation failure for task-lifecycle tests."""

    def __init__(self) -> None:
        super().__init__("accuracy transport failed")


class _CoordinatedEvaluation:
    """Evaluation fake that exposes sibling cancellation without wall-clock waits."""

    def __init__(self, *, fail_accuracy: bool) -> None:
        self.delegate = FakeEvaluation()
        self.fail_accuracy = fail_accuracy
        self.accuracy_started = asyncio.Event()
        self.benchmark_started = asyncio.Event()
        self.accuracy_task: asyncio.Task[object] | None = None
        self.benchmark_task: asyncio.Task[object] | None = None
        self.canceled_while_live: list[str] = []

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        del reuse
        self.accuracy_task = asyncio.current_task()
        self.accuracy_started.set()
        if self.fail_accuracy:
            await self.benchmark_started.wait()
            raise _EvaluationTransportError
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not getattr(workspace, "discarded", False):
                self.canceled_while_live.append("accuracy")
            raise
        return AccuracyEvaluation(executed=True)

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
    ) -> BenchmarkEvaluation:
        del objectives
        self.benchmark_task = asyncio.current_task()
        self.benchmark_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not getattr(workspace, "discarded", False):
                self.canceled_while_live.append("benchmark")
            raise
        return BenchmarkEvaluation(executed=True)

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        return await self.delegate.validate_local(
            workspace,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
        )


def test_options_and_portfolios_are_strict() -> None:
    with pytest.raises(ValidationError, match="max_in_flight"):
        _options(max_in_flight=0)
    with pytest.raises(ValidationError, match="unexpected"):
        _options(unexpected=True)
    with pytest.raises(ValidationError, match="distinct hypothesis IDs"):
        PortfolioPlan.model_validate(_portfolio("same", "same"))
    with pytest.raises(ValidationError, match="profiler"):
        PortfolioPlan.model_validate({**_portfolio("one"), "profiler": []})
    with pytest.raises(ValidationError, match="profiler"):
        DynamicState.model_validate({"profiler": []})


def test_profiler_role_is_read_only_resumable_and_evaluation_enabled() -> None:
    assert PROFILER in PLUGIN.agents
    assert PROFILER.workspace_access.value == "read_only"
    assert tuple(tool.id for tool in PROFILER.extra_tools) == ("evaluation", "profiler")
    assert PROFILER.required_capabilities == frozenset(
        {
            AgentCapability.MCP_SERVERS,
            AgentCapability.SESSION_REUSE,
            AgentCapability.PROVIDER_SESSION_RESUME,
        }
    )


def test_parallel_hypotheses_use_isolated_workspaces_and_adopt_best(tmp_path: Path) -> None:
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("prefill", "decode")],
            IMPLEMENTER.id: [_implementation("prefill"), _implementation("decode")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )

    async def scenario() -> tuple[RunStatus, FakeRun]:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                accuracy_configured=True,
                benchmark_configured=True,
            ),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=True,
        )
        run.evaluation.script_benchmark(
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=10.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 10.0},
            ),
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=12.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 12.0},
            ),
        )
        status = await PLUGIN.orchestrate(run, _options())
        return status, run

    status, run = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    assert len(run.workspaces.candidates) == 2
    assert all(candidate.discarded for candidate in run.workspaces.candidates)
    implementers = [session for session in run.agents.sessions if session.role.id == IMPLEMENTER.id]
    assert {session.member_id for session in implementers} == {"prefill", "decode"}
    assert len({session.workspace.id for session in implementers}) == 2
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.winner_revision == state.workstreams[1].candidate_revision
    assert state.adoption_pending is False
    assert run.workspaces.root.restore_calls[-1][0] == state.winner_revision
    assert [item.hypothesis_id for item in state.search.hypotheses] == ["prefill", "decode"]
    assert [item.round_number for item in state.search.rounds] == [1, 2]
    assert [item.perf_provenance for item in state.search.rounds] == [
        "framework",
        "framework",
    ]
    measurements = [item.measurement for item in state.search.hypotheses]
    assert all(item is not None for item in measurements)
    assert [item.value if item is not None else None for item in measurements] == [10.0, 12.0]


def test_nonparallel_runtime_limits_portfolio_to_one(tmp_path: Path) -> None:
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("first", "second"), _portfolio("first")],
            IMPLEMENTER.id: [_implementation("first")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> tuple[RunStatus, FakeRun]:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=False,
        )
        with pytest.raises(RuntimeError, match="does not support parallel candidate"):
            await PLUGIN.orchestrate(run, _options())
        return RunStatus.FAILED, run

    # The runtime contract has no isolated candidate facility in this mode. The
    # planner is corrected to one workstream before the explicit failure.
    _status, run = asyncio.run(scenario())
    orchestrator_sessions = [
        session for session in run.agents.sessions if session.role.id == ORCHESTRATOR.id
    ]
    assert len(orchestrator_sessions) == 1
    assert len(orchestrator_sessions[0].history) == 2


def test_later_epoch_continues_same_hypothesis_and_session_identity(tmp_path: Path) -> None:
    first = _portfolio("stream")
    second = _portfolio("stream", continue_hypothesis=True)
    script = _Script(
        {
            ORCHESTRATOR.id: [first, second],
            IMPLEMENTER.id: [
                {
                    "summary": "Established the mechanism and retained diagnostics.",
                    "outcome": "continue",
                    "next_step": "Apply the bounded source change.",
                },
                _implementation("stream"),
            ],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=True,
        )
        await PLUGIN.orchestrate(run, _options(max_rounds=2, max_in_flight=1))
        return run

    run = asyncio.run(scenario())
    implementers = [session for session in run.agents.sessions if session.role.id == IMPLEMENTER.id]
    assert [session.member_id for session in implementers] == ["stream", "stream"]
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert len(state.search.hypotheses) == 1
    assert [record.round_number for record in state.search.rounds] == [1, 2]
    assert [record.hypothesis_declared_outcome for record in state.search.rounds] == [
        "continue",
        "nominated",
    ]


@pytest.mark.parametrize(
    ("durable_phase", "commit_results", "judge_replies", "benchmark_results"),
    [
        pytest.param(
            "implemented",
            (None, None, None, RuntimeError("stop after implementation"), RuntimeError("stop")),
            2,
            1,
            id="implemented",
        ),
        pytest.param(
            "reviewed",
            (
                None,
                None,
                None,
                None,
                RuntimeError("stop after review"),
                RuntimeError("stop"),
            ),
            1,
            1,
            id="reviewed",
        ),
        pytest.param(
            "evaluated",
            (
                None,
                None,
                None,
                None,
                None,
                None,
                RuntimeError("stop after evaluation"),
                RuntimeError("stop"),
            ),
            1,
            1,
            id="evaluated",
        ),
    ],
)
def test_resume_completes_durable_work_without_repeating_finished_stages(
    tmp_path: Path,
    durable_phase: str,
    commit_results: tuple[BaseException | None, ...],
    judge_replies: int,
    benchmark_results: int,
) -> None:
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("recover")],
            IMPLEMENTER.id: [_implementation("recover")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."} for _ in range(judge_replies)
            ],
        }
    )

    async def scenario() -> tuple[FakeRun, RunStatus]:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                benchmark_configured=True,
            ),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=True,
        )
        run.evaluation.script_benchmark(
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=12.0,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": 12.0},
                )
                for _ in range(benchmark_results)
            )
        )
        run.state.script_commit(*commit_results)
        with pytest.raises(RuntimeError, match="stop"):
            await PLUGIN.orchestrate(run, _options(max_in_flight=1))
        interrupted = await run.state.load(DynamicState)
        assert interrupted is not None
        assert interrupted.workstreams[0].phase.value == durable_phase
        status = await PLUGIN.orchestrate(run, _options(max_in_flight=1))
        return run, status

    run, status = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    assert len([s for s in run.agents.sessions if s.role.id == ORCHESTRATOR.id]) == 1
    assert len([s for s in run.agents.sessions if s.role.id == IMPLEMENTER.id]) == 1
    assert len([s for s in run.agents.sessions if s.role.id == JUDGE.id]) == judge_replies
    assert len(run.evaluation.benchmark_calls) == benchmark_results
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].phase.value == "evaluated"
    assert state.eligible_evaluation_candidates == 1
    assert len(state.search.rounds) == 1
    assert state.adoption_pending is False


def test_resumed_rejected_evaluation_drives_a_correction_attempt(tmp_path: Path) -> None:
    feedback = "benchmark mismatch"
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("recover")],
            IMPLEMENTER.id: [_implementation("first"), _implementation("corrected")],
            JUDGE.id: [
                {"passed": True, "analysis": "First candidate is reviewable."},
                {"passed": True, "analysis": "Correction is reviewable."},
            ],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                benchmark_configured=True,
            ),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=True,
        )
        run.evaluation.script_benchmark(
            BenchmarkEvaluation(executed=True, feedback=feedback),
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=12.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 12.0},
            ),
        )
        run.state.script_commit(
            None,
            None,
            None,
            None,
            None,
            None,
            RuntimeError("stop after rejected evaluation"),
            RuntimeError("stop"),
        )
        with pytest.raises(RuntimeError, match="stop"):
            await PLUGIN.orchestrate(run, _options(max_retries_per_round=2))
        interrupted = await run.state.load(DynamicState)
        assert interrupted is not None
        assert interrupted.workstreams[0].phase.value == "evaluated"
        assert interrupted.workstreams[0].evaluation is not None
        assert not interrupted.workstreams[0].evaluation.accepted

        await PLUGIN.orchestrate(run, _options(max_retries_per_round=2))
        return run

    run = asyncio.run(scenario())

    implementer_messages = [message for role, _, message in script.calls if role == IMPLEMENTER.id]
    assert len(implementer_messages) == 2
    assert f"Trusted evaluation failed: {feedback}" in implementer_messages[1]
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].attempts == 2
    assert state.workstreams[0].evaluation is not None
    assert state.workstreams[0].evaluation.accepted


@pytest.mark.parametrize("cancel_orchestrate", [False, True], ids=["exception", "cancellation"])
def test_parallel_evaluations_are_drained_before_candidate_discard(
    tmp_path: Path,
    *,
    cancel_orchestrate: bool,
) -> None:
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("candidate")],
            IMPLEMENTER.id: [_implementation("candidate")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is reviewable."}],
        }
    )

    async def scenario() -> tuple[_CoordinatedEvaluation, FakeRun]:
        fake = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                accuracy_configured=True,
                benchmark_configured=True,
            ),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=True,
        )
        evaluation = _CoordinatedEvaluation(fail_accuracy=not cancel_orchestrate)
        run = Run(
            run_id=fake.run_id,
            facts=fake.facts,
            agents=fake.agents,
            workspaces=fake.workspaces,
            evaluation=evaluation,
            state=fake.state,
            control=fake.control,
            commands=fake.commands,
            skills=fake.skills,
            observations=fake.observations,
        )
        orchestrating = asyncio.ensure_future(PLUGIN.orchestrate(run, _options()))
        await evaluation.accuracy_started.wait()
        await evaluation.benchmark_started.wait()
        if cancel_orchestrate:
            orchestrating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await orchestrating
        else:
            await orchestrating
        return evaluation, fake

    evaluation, run = asyncio.run(scenario())

    expected = {"accuracy", "benchmark"} if cancel_orchestrate else {"benchmark"}
    assert set(evaluation.canceled_while_live) == expected
    assert evaluation.accuracy_task is not None
    assert evaluation.benchmark_task is not None
    assert evaluation.benchmark_task.done()
    assert all(candidate.discarded for candidate in run.workspaces.candidates)


def test_failed_parallel_slot_is_retried_without_canceling_its_sibling(tmp_path: Path) -> None:
    attempts = {"fragile": 0, "steady": 0}

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            return _portfolio("fragile", "steady")
        hypothesis_id = "fragile" if "fragile" in message else "steady"
        if role.id == IMPLEMENTER.id:
            attempts[hypothesis_id] += 1
            return _implementation(hypothesis_id)
        if hypothesis_id == "fragile" and attempts[hypothesis_id] == 1:
            raise _JudgeTransportError
        return {"passed": True, "analysis": "Candidate is correct."}

    async def scenario() -> FakeRun:
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
        run.evaluation.script_benchmark(
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=value,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": value},
                )
                for value in (10.0, 11.0)
            )
        )
        await PLUGIN.orchestrate(run, _options(max_retries_per_round=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert attempts == {"fragile": 2, "steady": 1}
    assert all(item.phase.value == "evaluated" for item in state.workstreams)
    assert any(commit.label == "dynamic: fragile failed" for commit in run.state.commits)


@pytest.mark.parametrize("gate", ["local", "accuracy", "benchmark"])
def test_evaluation_failure_feedback_drives_a_correction_attempt(
    tmp_path: Path,
    gate: str,
) -> None:
    feedback = f"{gate} mismatch"
    first_implementation = _implementation("first")
    corrected_implementation = _implementation("corrected")
    if gate == "local":
        first_implementation["validation_recipe_artifact"] = "validation/recipe.json"
        corrected_implementation["validation_recipe_artifact"] = "validation/recipe.json"
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("correct")],
            IMPLEMENTER.id: [first_implementation, corrected_implementation],
            JUDGE.id: [
                {"passed": True, "analysis": "First candidate is reviewable."},
                {"passed": True, "analysis": "Correction is reviewable."},
            ],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve.",
                accuracy_configured=gate == "accuracy",
                benchmark_configured=gate == "benchmark",
            ),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        if gate == "local":
            run.evaluation.script_local_validation(
                LocalValidationEvaluation(passed=False, feedback=feedback)
            )
        elif gate == "accuracy":
            run.evaluation.script_accuracy(
                AccuracyEvaluation(executed=True, feedback=feedback),
                AccuracyEvaluation(executed=True),
            )
        else:
            run.evaluation.script_benchmark(
                BenchmarkEvaluation(executed=True, feedback=feedback),
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=10.0,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": 10.0},
                ),
            )
        await PLUGIN.orchestrate(run, _options(max_retries_per_round=2))
        return run

    run = asyncio.run(scenario())
    implementer_messages = [message for role, _, message in script.calls if role == IMPLEMENTER.id]
    assert len(implementer_messages) == 2
    assert f"Trusted evaluation failed: {feedback}" in implementer_messages[1]
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].attempts == 2
    assert state.workstreams[0].evaluation is not None
    assert state.workstreams[0].evaluation.accepted


def test_evaluation_cadence_counts_eligible_candidates_not_successes(tmp_path: Path) -> None:
    script = _Script(
        {
            ORCHESTRATOR.id: [
                _portfolio("first", "second", request_evaluation=False),
                _portfolio("terminal", request_evaluation=False),
            ],
            IMPLEMENTER.id: [
                _implementation("first"),
                _implementation("second"),
                {"summary": "No viable change.", "outcome": "disproven"},
            ],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        run.evaluation.script_benchmark(
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=10.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 10.0},
            )
        )
        await PLUGIN.orchestrate(run, _options(max_rounds=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.eligible_evaluation_candidates == 2
    assert len(run.evaluation.benchmark_calls) == 1


def test_portfolio_history_omits_large_evaluation_feedback(tmp_path: Path) -> None:
    diagnostic = "benchmark failure\n" * 10_000
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("failed"), _portfolio("terminal")],
            IMPLEMENTER.id: [
                _implementation("failed"),
                {"summary": "No viable follow-up.", "outcome": "disproven"},
            ],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        run.evaluation.script_benchmark(BenchmarkEvaluation(executed=True, feedback=diagnostic))
        await PLUGIN.orchestrate(run, _options(max_rounds=2, max_in_flight=1))
        return run

    asyncio.run(scenario())
    orchestrator_messages = [
        message for role, _, message in script.calls if role == ORCHESTRATOR.id
    ]
    assert len(orchestrator_messages) == 2
    assert diagnostic not in orchestrator_messages[1]
    assert len(orchestrator_messages[1]) < 10_000


def test_noise_aware_multi_axis_frontier_drives_dispositions_and_winner(
    tmp_path: Path,
) -> None:
    script = _Script(
        {
            ORCHESTRATOR.id: [_portfolio("fast", "lean", "dominated")],
            IMPLEMENTER.id: [
                _implementation("fast"),
                _implementation("lean"),
                _implementation("dominated"),
            ],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )
    rows = (
        {"throughput": 100.0, "latency": 10.0},
        {"throughput": 90.0, "latency": 8.0},
        {"throughput": 99.0, "latency": 11.0},
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        run.evaluation.script_benchmark(
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=row["throughput"],
                    metric_direction=MetricDirection.MAXIMIZE,
                    row=row,
                )
                for row in rows
            )
        )
        await PLUGIN.orchestrate(
            run,
            _options(
                max_in_flight=3,
                metric_space={
                    "relative_noise": 0.05,
                    "objectives": [
                        {"name": "throughput", "direction": "max"},
                        {"name": "latency", "direction": "min"},
                    ],
                },
            ),
        )
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    dispositions = {
        record.hypothesis_id: record.candidate_disposition for record in state.search.rounds
    }
    assert dispositions == {
        "fast": "pareto_frontier",
        "lean": "pareto_frontier",
        "dominated": "discard",
    }
    winner = next(item for item in state.workstreams if item.hypothesis_id == "fast")
    assert state.winner_revision == winner.candidate_revision


def test_failed_slot_sequence_is_not_reused_by_a_later_winner(tmp_path: Path) -> None:
    """A slot that failed before recording a round keeps its sequence to itself.

    Otherwise a later workstream reuses the number, and the winner lookup by
    round number resolves to the failed slot's unreviewed revision.
    """

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            return _portfolio("winner") if "epoch 2" in message else _portfolio("base", "broken")
        hypothesis_id = next(
            name for name in ("base", "broken", "winner") if f"`{name}`" in message
        )
        if role.id == IMPLEMENTER.id:
            return _implementation(hypothesis_id)
        if hypothesis_id == "broken":
            raise _JudgeTransportError
        return {"passed": True, "analysis": "Candidate is correct."}

    async def scenario() -> FakeRun:
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
        run.evaluation.script_benchmark(
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=value,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": value},
                )
                for value in (10.0, 20.0)
            )
        )
        await PLUGIN.orchestrate(run, _options(max_rounds=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    sequences = [item.sequence for item in state.workstreams]
    assert len(sequences) == len(set(sequences))
    winner = next(item for item in state.workstreams if item.hypothesis_id == "winner")
    assert state.winner_revision == winner.candidate_revision


_FATES = st.sampled_from(("raise", "reject", "pass"))


@settings(max_examples=30, deadline=None)
@given(epochs=st.lists(st.tuples(_FATES, _FATES), min_size=1, max_size=3))
def test_winner_is_always_the_workstream_of_the_winning_round(
    epochs: list[tuple[str, str]],
) -> None:
    """For any mix of failed, rejected, and evaluated slots, adoption is consistent.

    Sequences stay unique, and the adopted revision is the candidate of the
    workstream that recorded the best trusted round.
    """
    fates = {
        f"e{epoch}-{slot}": fate
        for epoch, pair in enumerate(epochs, start=1)
        for slot, fate in zip("ab", pair, strict=True)
    }
    planned = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal planned
        if role.id == ORCHESTRATOR.id:
            planned += 1
            return _portfolio(f"e{planned}-a", f"e{planned}-b")
        hypothesis_id = next(name for name in fates if f"`{name}`" in message)
        if role.id == IMPLEMENTER.id:
            return _implementation(hypothesis_id)
        if fates[hypothesis_id] == "raise":
            raise _JudgeTransportError
        passed = fates[hypothesis_id] == "pass"
        return {"passed": passed, "analysis": "Reviewed.", "feedback": "" if passed else "No."}

    async def scenario(root: Path) -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=root,
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
        run.evaluation.script_benchmark(
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=value,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": value},
                )
                for value in (10.0 * (index + 1) for index in range(len(fates)))
            )
        )
        await PLUGIN.orchestrate(run, _options(max_rounds=len(epochs)))
        return run

    with tempfile.TemporaryDirectory() as directory:
        run = asyncio.run(scenario(Path(directory)))
        state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    sequences = [item.sequence for item in state.workstreams]
    assert len(sequences) == len(set(sequences))
    trusted = [
        record
        for record in state.search.rounds
        if record.official_evaluation and record.perf_metric is not None
    ]
    if not trusted:
        assert state.winner_revision is None
        return
    best = max(trusted, key=lambda record: record.perf_metric or 0.0)
    winner = next(item for item in state.workstreams if item.hypothesis_id == best.hypothesis_id)
    assert state.winner_revision == winner.candidate_revision == best.commit


def test_recorded_hypothesis_lineage_matches_the_branched_revision(tmp_path: Path) -> None:
    """Each hypothesis records the revision its workstream branched from, not a sibling."""
    planned = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal planned
        if role.id == ORCHESTRATOR.id:
            planned += 1
            return _portfolio(f"e{planned}-a", f"e{planned}-b", request_evaluation=False)
        return {"summary": "No viable change.", "outcome": "disproven"}

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        await PLUGIN.orchestrate(run, _options(max_rounds=2, judge_every=100))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    hypotheses = {item.hypothesis_id: item for item in state.search.hypotheses}
    rounds = {record.hypothesis_id: record.round_number for record in state.search.rounds}
    assert len(hypotheses) == len(state.workstreams) == 4
    for workstream in state.workstreams:
        hypothesis = hypotheses[workstream.hypothesis_id]
        assert hypothesis.parent_commit == workstream.parent_revision
        assert hypothesis.parent_round is None
        assert rounds[workstream.hypothesis_id] == workstream.sequence


def test_projected_round_budget_covers_every_recorded_round(tmp_path: Path) -> None:
    """The run's advertised round budget matches the rounds a full run records.

    Each epoch records one round per workstream, so a budget of epochs alone
    would show progress past 100%.
    """
    planned = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal planned
        if role.id == ORCHESTRATOR.id:
            planned += 1
            return _portfolio(f"e{planned}-a", f"e{planned}-b", request_evaluation=False)
        return {"summary": "No viable change.", "outcome": "disproven"}

    options = _options(max_rounds=2, max_in_flight=2, judge_every=100)

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        await PLUGIN.orchestrate(run, options)
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert REGISTRATION.project_max_rounds is not None
    budget = REGISTRATION.project_max_rounds(options)
    assert max(record.round_number for record in state.search.rounds) == budget
