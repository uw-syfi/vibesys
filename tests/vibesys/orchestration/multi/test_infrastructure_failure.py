"""An infrastructure benchmark failure is the machinery's, not the candidate's.

``BenchmarkEvaluation.failure_kind`` says whose fault a failed benchmark is. The multi-agent
loop measures an infrastructure failure again; it never spends a paid implementer attempt on
it or hands the machinery's failure text to the implementer as something to repair.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi import PLUGIN
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.review import Verdict
from vs_core.api import DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentCapability,
    BenchmarkEvaluation,
    BenchmarkFailureKind,
    RunFacts,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    import pytest
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_DESIGNER, _PROFILER, IMPLEMENTER, _JUDGE = PLUGIN.agents
_CAPABILITIES = frozenset(
    {
        AgentCapability.PROVIDER_SESSION_RESUME,
        AgentCapability.SESSION_REUSE,
        AgentCapability.MCP_SERVERS,
    }
)
_FACTS = RunFacts(
    domain_id="generic",
    objective="Improve the candidate.",
    accuracy_configured=True,
    benchmark_configured=True,
)
_PRE_ROUND = PreRoundDecision.model_validate(
    {"need_profile": False, "profile_focus": "", "reasoning": "Evidence is sufficient."}
)
_PLAN = OrchestratorPlan.model_validate(
    {
        "hypothesis_id": "H-01",
        "hypothesis": "Batching removes per-request overhead.",
        "title": "Batch prefill",
        "task": "Batch prefill requests.",
        "pass_criteria": "Throughput improves without an accuracy regression.",
        "reasoning": "The trace shows repeated launch overhead.",
    }
)
_IMPLEMENTATION = ImplementerResponse.model_validate(
    {
        "summary": "Implemented batching.",
        "expected_behavior": "Fewer launches.",
        "hypothesis_outcome": "nominated",
        "evidence": "The local smoke check passed.",
    }
)
_JUDGE_PASS = JudgeResponse.model_validate(
    {"analysis": "Matches the plan.", "feedback": "", "verdict": Verdict.PASS}
)
_MEASURED = BenchmarkEvaluation(executed=True, metric_name="throughput", metric_value=80.0)
_CAUSE = "node lost: the allocation ended before the benchmark reported"


class _Script:
    """The designer plans once; each implementer attempt is nominated and approved."""

    def __init__(self) -> None:
        self.replies: deque[object] = deque(
            (_PRE_ROUND, _PLAN, *[_IMPLEMENTATION, _JUDGE_PASS] * 4)
        )
        self.calls: list[tuple[str, str]] = []

    def respond(
        self,
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, message))
        return self.replies.popleft()


def _run_round(
    tmp_path_factory: pytest.TempPathFactory, readings: list[BenchmarkEvaluation], retries: int
) -> tuple[_Script, FakeRun]:
    script = _Script()
    root = tmp_path_factory.mktemp("multi")

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=root,
            facts=_FACTS,
            responder=script.respond,
            supported_agent_capabilities=_CAPABILITIES,
            supported_extra_tools=("profiler",),
        )
        run.evaluation.script_benchmark(*readings)
        try:
            options = PLUGIN.options.model_validate(
                {
                    "interface": "service",
                    "max_rounds": 1,
                    "max_retries_per_round": retries,
                    "judge_every": 1,
                    "official_eval_every": 1,
                }
            )
            await PLUGIN.orchestrate(run, options)
            return run
        finally:
            await run.close()

    return script, asyncio.run(scenario())


def _infrastructure(kind: BenchmarkFailureKind) -> BenchmarkEvaluation:
    return BenchmarkEvaluation(executed=False, feedback=_CAUSE, failure_kind=kind)


@settings(max_examples=6)
@given(
    retries=st.integers(min_value=1, max_value=3),
    lost=st.integers(min_value=1, max_value=DEFAULT_MAX_MEASUREMENT_SUBMISSIONS - 1),
)
def test_a_lost_benchmark_is_measured_again_without_a_paid_attempt(
    tmp_path_factory: pytest.TempPathFactory, retries: int, lost: int
) -> None:
    readings = [*[_infrastructure(BenchmarkFailureKind.INFRASTRUCTURE)] * lost, _MEASURED]

    script, run = _run_round(tmp_path_factory, readings, retries)

    implementer = [message for role, message in script.calls if role == IMPLEMENTER.id]
    assert len(implementer) == 1
    assert len(run.evaluation.benchmark_calls) == lost + 1


@settings(max_examples=3)
@given(retries=st.integers(min_value=2, max_value=3))
def test_a_benchmark_lost_beyond_the_bound_ends_the_round_without_repair_feedback(
    tmp_path_factory: pytest.TempPathFactory, retries: int
) -> None:
    readings = [_infrastructure(BenchmarkFailureKind.INFRASTRUCTURE)] * (
        DEFAULT_MAX_MEASUREMENT_SUBMISSIONS * retries
    )

    script, run = _run_round(tmp_path_factory, readings, retries)

    implementer = [message for role, message in script.calls if role == IMPLEMENTER.id]
    assert len(implementer) == 1
    assert len(run.evaluation.benchmark_calls) == DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
    assert not any(_CAUSE in message for message in implementer)


@settings(max_examples=3)
@given(
    retries=st.integers(min_value=2, max_value=3),
    lost=st.integers(min_value=1, max_value=DEFAULT_MAX_MEASUREMENT_SUBMISSIONS),
)
def test_a_lost_accuracy_is_measured_again_then_ends_the_round_unmeasured(
    tmp_path_factory: pytest.TempPathFactory, retries: int, lost: int
) -> None:
    script = _Script()
    root = tmp_path_factory.mktemp("multi")
    gone = AccuracyEvaluation(
        executed=False, feedback=_CAUSE, failure_kind=BenchmarkFailureKind.INFRASTRUCTURE
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=root,
            facts=_FACTS,
            responder=script.respond,
            supported_agent_capabilities=_CAPABILITIES,
            supported_extra_tools=("profiler",),
        )
        run.evaluation.script_accuracy(*[gone] * lost, AccuracyEvaluation(executed=True))
        run.evaluation.script_benchmark(_MEASURED)
        try:
            options = PLUGIN.options.model_validate(
                {
                    "interface": "service",
                    "max_rounds": 1,
                    "max_retries_per_round": retries,
                    "judge_every": 1,
                    "official_eval_every": 1,
                }
            )
            await PLUGIN.orchestrate(run, options)
            return run
        finally:
            await run.close()

    run = asyncio.run(scenario())

    implementer = [message for role, message in script.calls if role == IMPLEMENTER.id]
    assert len(implementer) == 1
    assert not any(_CAUSE in message for message in implementer)
    if lost < DEFAULT_MAX_MEASUREMENT_SUBMISSIONS:
        assert len(run.evaluation.accuracy_calls) == lost + 1
        assert len(run.evaluation.benchmark_calls) == 1
    else:
        assert len(run.evaluation.accuracy_calls) == DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
        assert run.evaluation.benchmark_calls == []
