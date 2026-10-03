"""One workstream's stages: implementation, review, evaluation, and correction."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    EvaluationTransportError,
    JudgeTransportError,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    requested_slots,
    throughput,
)

from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicState,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentCapability,
    BenchmarkEvaluation,
    LocalValidationEvaluation,
    MetricDirection,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


def test_continued_hypothesis_resumes_its_session_in_a_reset_worktree(tmp_path: Path) -> None:
    """A continuation in a new epoch continues the implementer's conversation.

    The candidate path is keyed by hypothesis, so the provider session resumes;
    the prompt says the worktree was reset to the new parent and still carries
    the prior attempt for a session the provider could not resume.
    """
    first = {
        "summary": "Added a prefix cache; prefill time halved locally.",
        "outcome": "continue",
        "next_step": "Batch decode across sessions.",
        "evidence": [{"location": "evidence/prefix.json", "purpose": "cache hit rate"}],
    }
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("cache"),
                portfolio("cache", continue_hypothesis=True),
            ],
            IMPLEMENTER.id: [first, implementation("cache")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=True,
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))

    asyncio.run(scenario())
    first_prompt, continued_prompt = [
        message for role, _, message in script.calls if role == IMPLEMENTER.id
    ]
    implementer_histories = [
        history for role, history in script.histories if role == IMPLEMENTER.id
    ]
    assert implementer_histories == [(), (first_prompt,)]
    assert "it was recreated at" in continued_prompt
    assert "That is the revision your earlier attempt ended at" in continued_prompt
    assert "earlier attempt" not in first_prompt
    assert "earlier attempt" in continued_prompt
    assert first["summary"] in continued_prompt
    assert first["next_step"] in continued_prompt
    assert "evidence/prefix.json" in continued_prompt


def test_later_epoch_continues_same_hypothesis_and_session_identity(tmp_path: Path) -> None:
    first = portfolio("stream")
    second = portfolio("stream", continue_hypothesis=True)
    script = Script(
        {
            ORCHESTRATOR.id: [first, second],
            IMPLEMENTER.id: [
                {
                    "summary": "Established the mechanism and retained diagnostics.",
                    "outcome": "continue",
                    "next_step": "Apply the bounded source change.",
                },
                implementation("stream"),
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
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1))
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


@pytest.mark.parametrize("gate", ["local", "accuracy", "benchmark"])
def test_evaluation_failure_feedback_drives_a_correction_attempt(
    tmp_path: Path,
    gate: str,
) -> None:
    feedback = f"{gate} mismatch"
    first_implementation = implementation("first")
    corrected_implementation = implementation("corrected")
    if gate == "local":
        first_implementation["validation_recipe_artifact"] = "validation/recipe.json"
        corrected_implementation["validation_recipe_artifact"] = "validation/recipe.json"
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("correct")],
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
                INPUT_BASELINE,
                BenchmarkEvaluation(executed=True, feedback=feedback),
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=10.0,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": 10.0},
                ),
            )
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_retries_per_round=2))
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


def test_every_reviewed_candidate_gets_a_trusted_evaluation(tmp_path: Path) -> None:
    # Neither the planner nor the cadence (every 2nd candidate) asks for these
    # evaluations, and the first epoch is not the final one.
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("first", "second"),
                # Both slots free together; the plan fills both.
                portfolio("terminal", "last"),
            ],
            IMPLEMENTER.id: [
                implementation("first"),
                implementation("second"),
                {"summary": "No viable change.", "outcome": "disproven"},
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
            INPUT_BASELINE,
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=value,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": value},
                )
                for value in (10.0, 12.0)
            ),
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert len(run.evaluation.benchmark_calls) == 1 + 2
    first, second = state.workstreams[:2]
    assert [first.phase.value, second.phase.value] == ["evaluated", "evaluated"]
    # Without its evaluation, the better reviewed candidate could not win.
    assert state.winner_revision == second.candidate_revision


def test_failed_evaluation_is_retried_without_reimplementing(tmp_path: Path) -> None:
    """A stage failure after a retained implementation resumes at that stage."""
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("stable")],
            IMPLEMENTER.id: [implementation("stable")],
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
        # Call 1 measures the input baseline; the candidate's first fails in transport.
        run.evaluation.script_benchmark(
            INPUT_BASELINE,
            EvaluationTransportError(),
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=10.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 10.0},
            ),
        )
        status = await PLUGIN.orchestrate(
            run, dynamic_options(max_in_flight=1, max_retries_per_round=2)
        )
        assert status is RunStatus.SUCCEEDED
        return run

    run = asyncio.run(scenario())
    assert len(run.evaluation.benchmark_calls) == 1 + 2
    assert len([s for s in run.agents.sessions if s.role.id == IMPLEMENTER.id]) == 1
    assert len([s for s in run.agents.sessions if s.role.id == JUDGE.id]) == 1
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].attempts == 1
    assert state.workstreams[0].phase.value == "evaluated"
    assert state.winner_revision == state.workstreams[0].candidate_revision


def test_retry_after_a_crashed_attempt_keeps_review_feedback_and_says_the_tree_was_reset(
    tmp_path: Path,
) -> None:
    """A slot retry resumes the implementer's session in a recreated worktree.

    Attempt 1 is rejected by review; attempt 2's turn fails. The retry starts
    from attempt 1's retained candidate, says so, and still carries the review
    feedback that attempt 2 never acted on.
    """
    rejection = "fix X: the cache is never invalidated"
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [
                implementation("cache"),
                JudgeTransportError("implementer turn failed"),
                implementation("cache"),
            ],
            JUDGE.id: [
                {"passed": False, "analysis": "Stale entries.", "feedback": rejection},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )

    async def scenario() -> DynamicState | None:
        run = baseline_run(tmp_path, script)
        run.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        status = await PLUGIN.orchestrate(
            run, dynamic_options(max_rounds=1, max_in_flight=1, max_retries_per_round=3)
        )
        assert status is RunStatus.SUCCEEDED
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())

    prompts = [message for role, _, message in script.calls if role == IMPLEMENTER.id]
    histories = [history for role, history in script.histories if role == IMPLEMENTER.id]
    assert len(prompts) == 3
    retry = " ".join(prompts[2].split())
    assert histories[2], "the retry resumes the implementer's conversation"
    assert "it was recreated at" in retry
    assert "That is the revision your earlier attempt ended at" in retry
    assert rejection in retry
    assert state is not None
    assert state.workstreams[0].feedback is None
    assert state.winner_revision == state.workstreams[0].candidate_revision


@settings(max_examples=8, deadline=None)
@given(judge_every=st.integers(min_value=1, max_value=4))
def test_terminal_outcomes_are_reviewed_on_every_judge_every_th_workstream(
    tmp_path_factory: pytest.TempPathFactory,
    judge_every: int,
) -> None:
    """`judge_every` counts workstreams (rounds), not planning calls.

    Under slot refill a planning call fills only the free slots, so counting
    calls would review a different, scheduling-dependent set of workstreams.
    """
    planned = 0
    reviewed: list[str] = []

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal planned
        hypothesis_id = next((n for n in message.split("`") if n.startswith("h")), "")
        if role.id == ORCHESTRATOR.id:
            slots = []
            for _slot in range(requested_slots(message)):
                planned += 1
                slots.append(f"h{planned}")
            return portfolio(*slots)
        if role.id == IMPLEMENTER.id:
            return {"summary": f"Disproved {hypothesis_id}.", "outcome": "disproven"}
        reviewed.append(hypothesis_id)
        return {"passed": True, "analysis": "The negative result holds."}

    async def scenario() -> DynamicState | None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path_factory.mktemp("run"),
            facts=RunFacts(domain_id="generic", objective="Improve."),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        options = dynamic_options(max_rounds=3, max_in_flight=2, judge_every=judge_every)
        assert await PLUGIN.orchestrate(run, options) is RunStatus.SUCCEEDED
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())
    assert state is not None
    assert len(state.workstreams) == 6
    assert sorted(reviewed) == sorted(
        item.hypothesis_id for item in state.workstreams if item.sequence % judge_every == 0
    )
