"""Byte-exact goldens for the text the dynamic orchestration sends to its agents.

Each scenario runs the plugin through its public ``orchestrate`` entry point
over a ``FakeRun`` and snapshots every agent message in call order, so the
goldens pin the composed prompt, including correction and feedback text, not
one helper's output.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as
a prompt diff.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import PLUGIN, ImplementPortfolioPlan
from vibesys.orchestration.dynamic.agents import AGENTS, IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentCapability,
    AgentEvaluation,
    AgentEvaluationStatus,
    RunFacts,
    StructuredResponseError,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runtime.api import AgentRole

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "prompts"
_CAUSE = "ValueError: sequence length 22 exceeds state capacity 21"
_PASS = {"passed": True, "analysis": "Candidate is correct."}


def _check(name: str, actual: str) -> None:
    path = _SNAPSHOT_DIR / f"{name}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == actual


def _transcript(script: Script, tmp_path: Path) -> str:
    """Every agent message in call order, with the run's temporary root normalized."""
    return "".join(f"===== {role} =====\n{message}\n" for role, _, message in script.calls).replace(
        str(tmp_path), "<tmp>"
    )


def _run(
    tmp_path: Path,
    script: Script,
    facts: RunFacts,
    *,
    submissions: list[list[AgentEvaluation]] | None = None,
    setup: Callable[[FakeRun], None] | None = None,
    **options: object,
) -> None:
    """Run the plugin; the n-th implementer turn submits ``submissions[n]``."""
    turns = iter(submissions or [])
    holder: list[FakeRun] = []

    def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id:
            run = holder[0]
            workspace = run.workspaces.candidates[-1]
            for evaluation in next(turns, []):
                run.evaluation.record_agent_evaluation(workspace, evaluation)
        return script.respond(role, history, message, response)

    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=facts,
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        holder.append(run)
        if setup is not None:
            setup(run)
        await PLUGIN.orchestrate(run, dynamic_options(**options))

    asyncio.run(scenario())


_ACCURACY = RunFacts(domain_id="generic", objective="Improve.", accuracy_configured=True)
_BENCHMARK = RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True)


def _failed(failure: str, revision: str = "r-failed") -> AgentEvaluation:
    return AgentEvaluation(
        revision=revision,
        kinds=("accuracy",),
        status=AgentEvaluationStatus.FAILED,
        failure=failure,
    )


@pytest.mark.parametrize("role", AGENTS, ids=lambda role: role.id)
def test_system_prompts(role: AgentRole) -> None:
    _check(f"system_{role.id}", role.system_prompt)


def test_judge_and_retry_see_submitted_failures(tmp_path: Path) -> None:
    """Sites: the judge's evaluation list and the retry's appended failures."""
    long_failure = "HEAD-OF-A-LONG-LOG\n" + "noise line\n" * 2000 + _CAUSE
    passed = AgentEvaluation(
        revision="r-passed", kinds=("accuracy", "benchmark"), status=AgentEvaluationStatus.PASSED
    )
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache"), implementation("cache-fixed")],
            JUDGE.id: [
                {"passed": False, "analysis": "Fails its evaluation.", "feedback": "Fix it."},
                _PASS,
            ],
        }
    )

    _run(
        tmp_path,
        script,
        _ACCURACY,
        submissions=[[passed, _failed(long_failure), _failed("short failure", "r-2")], []],
        max_in_flight=1,
        max_retries_per_round=2,
    )

    _check("judge_and_retry_failures", _transcript(script, tmp_path))


def test_retry_after_failures_without_review_feedback(tmp_path: Path) -> None:
    """A rejected review with no feedback text: the retry carries only the failures."""
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache"), implementation("cache-fixed")],
            JUDGE.id: [{"passed": False, "analysis": "Fails."}, _PASS],
        }
    )

    _run(
        tmp_path,
        script,
        _ACCURACY,
        submissions=[[_failed("short failure")], []],
        max_in_flight=1,
        max_retries_per_round=2,
    )

    _check("retry_failures_only", _transcript(script, tmp_path))


def test_repeated_failure_ends_the_attempt(tmp_path: Path) -> None:
    signed = AgentEvaluation(
        revision="r-442",
        kinds=("accuracy",),
        status=AgentEvaluationStatus.FAILED,
        failure=f"Traceback ... line 442\n{_CAUSE}",
        signature="ValueError at model.py:442",
    )
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache"), implementation("cache-fixed")],
            JUDGE.id: [_PASS, _PASS],
        }
    )

    _run(
        tmp_path,
        script,
        _ACCURACY,
        submissions=[[signed] * 3, []],
        max_in_flight=1,
        max_retries_per_round=2,
    )

    _check("repeated_failure", _transcript(script, tmp_path))


# An empty gate message still fails the gate and yields the "no feedback" text.
@pytest.mark.parametrize("feedback", ["accuracy mismatch", ""], ids=["feedback", "no-feedback"])
def test_trusted_evaluation_failure_feedback(tmp_path: Path, feedback: str) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("correct")],
            IMPLEMENTER.id: [implementation("first"), implementation("corrected")],
            JUDGE.id: [_PASS, _PASS],
        }
    )

    def setup(run: FakeRun) -> None:
        run.evaluation.script_accuracy(
            AccuracyEvaluation(executed=True, feedback=feedback),
            AccuracyEvaluation(executed=True),
        )

    _run(
        tmp_path,
        script,
        _ACCURACY,
        setup=setup,
        max_in_flight=1,
        max_retries_per_round=2,
    )

    _check(
        f"trusted_evaluation_failure_{'feedback' if feedback else 'empty'}",
        _transcript(script, tmp_path),
    )


def test_unparseable_replies_are_corrected(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [
                # A run that cannot profile is offered implement workstreams only.
                StructuredResponseError(ORCHESTRATOR.id, ImplementPortfolioPlan),
                portfolio("recover"),
            ],
            IMPLEMENTER.id: [
                StructuredResponseError(IMPLEMENTER.id, BaseModel),
                implementation("recover"),
            ],
            JUDGE.id: [StructuredResponseError(JUDGE.id, BaseModel), _PASS],
        }
    )

    _run(
        tmp_path,
        script,
        _BENCHMARK,
        setup=lambda run: run.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0)),
        max_in_flight=1,
    )

    _check("structured_correction", _transcript(script, tmp_path))


def test_invalid_plan_is_corrected(tmp_path: Path) -> None:
    ghost = portfolio("ghost", continue_hypothesis=True)
    script = Script(
        {
            ORCHESTRATOR.id: [ghost, portfolio("a")],
            IMPLEMENTER.id: [implementation("a")],
            JUDGE.id: [_PASS],
        }
    )

    _run(
        tmp_path,
        script,
        _BENCHMARK,
        setup=lambda run: run.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0)),
        max_rounds=1,
        max_in_flight=1,
    )

    _check("plan_correction", _transcript(script, tmp_path))


def test_underfilled_plan_is_asked_to_fill_free_slots(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a"), portfolio("a", "b")],
            IMPLEMENTER.id: [implementation("a"), implementation("b")],
            JUDGE.id: [_PASS, _PASS],
        }
    )

    _run(
        tmp_path,
        script,
        _BENCHMARK,
        setup=lambda run: run.evaluation.script_benchmark(
            INPUT_BASELINE, throughput(10.0), throughput(11.0)
        ),
        max_rounds=1,
        max_in_flight=2,
    )

    # Only the planner's turns: parallel workstreams interleave their turns.
    planner = Script({})
    planner.calls = [call for call in script.calls if call[0] == ORCHESTRATOR.id]
    _check("plan_free_slots", _transcript(planner, tmp_path))


def test_planner_sees_buildable_candidates_and_the_parks_it_applied(tmp_path: Path) -> None:
    """Sites: the buildable-candidate list and a row's strategy.

    ``a`` passes accuracy but fails its benchmark, so it is not adopted and
    the base stays the input; ``b`` builds on it by naming it as its parent.
    """
    children = portfolio("b")["workstreams"]
    assert isinstance(children, list)
    (child,) = children
    building = {
        "reasoning": "Build on the correct but slow candidate.",
        "workstreams": [{**child, "parent_hypothesis_id": "a"}],
        "hypothesis_updates": [
            {"hypothesis_id": "a", "disposition": "parked", "reason": "Too slow alone."}
        ],
    }
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a"), building],
            IMPLEMENTER.id: [implementation("a"), implementation("b")],
            JUDGE.id: [_PASS, _PASS],
        }
    )
    slow = throughput(4.0).model_copy(update={"feedback": "warmup timed out at 4 requests/s"})

    def setup(run: FakeRun) -> None:
        run.evaluation.script_accuracy(
            AccuracyEvaluation(executed=True), AccuracyEvaluation(executed=True)
        )
        run.evaluation.script_benchmark(INPUT_BASELINE, slow, throughput(12.0))

    _run(
        tmp_path,
        script,
        RunFacts(
            domain_id="generic",
            objective="Improve.",
            accuracy_configured=True,
            benchmark_configured=True,
        ),
        setup=setup,
        max_rounds=2,
        max_in_flight=1,
    )

    implementer = [message for role, _, message in script.calls if role == IMPLEMENTER.id]
    planner = Script({})
    planner.calls = [call for call in script.calls if call[0] == ORCHESTRATOR.id]
    _check("plan_buildable_and_parked", _transcript(planner, tmp_path))
    # b starts from a's candidate, not from the unchanged input.
    assert "Parent revision: `candidate-1-revision-3`" in implementer[1]
