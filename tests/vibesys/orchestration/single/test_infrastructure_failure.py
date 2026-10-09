"""An infrastructure benchmark failure is the machinery's, not the candidate's.

``BenchmarkEvaluation.failure_kind`` says whose fault a failed benchmark is. The dynamic
loop measures an ``INFRASTRUCTURE`` failure again without reimplementing (#1113, #1365).
The single-agent loop must not instead charge it to the candidate: spend a paid
implementer attempt on it, send the machinery's failure text to the implementer as
something to repair, or close the round without measuring the candidate again.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.hypothesis import OrchestratorPlan
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single import PLUGIN
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vs_runtime.api import (
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

_DESIGNER, IMPLEMENTER = PLUGIN.agents
_CAPABILITIES = frozenset({AgentCapability.PROVIDER_SESSION_RESUME, AgentCapability.SESSION_REUSE})
_FACTS = RunFacts(
    domain_id="generic",
    objective="Improve the candidate.",
    accuracy_configured=True,
    benchmark_configured=True,
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
_RESPONSE = SingleAgentRoundResponse.model_validate(
    {
        "summary": "Implemented batching.",
        "expected_behavior": "Fewer launches.",
        "self_review": "Correctness checks passed.",
        "feedback": "",
        "verdict": Verdict.PASS,
        "bottlenecks": "Launch overhead.",
        "suggestions": "Try larger batches.",
        "profile_analysis": "Launch time fell.",
    }
)
_MEASURED = BenchmarkEvaluation(executed=True, metric_name="throughput", metric_value=80.0)


class _Script:
    """The designer plans once; the implementer passes its own review every time."""

    def __init__(self) -> None:
        self.replies: deque[object] = deque((_PLAN, *([_RESPONSE] * 8)))
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


@settings(max_examples=10)
@given(
    retries=st.integers(min_value=1, max_value=3),
    cause=st.sampled_from(
        (
            "node lost: the allocation ended before the benchmark reported",
            "benchmark command could not be executed: connection reset",
            "Model-weight request could not be satisfied: volume busy",
        )
    ),
)
def test_an_infrastructure_benchmark_failure_is_measured_again_without_a_paid_attempt(
    tmp_path_factory: pytest.TempPathFactory, retries: int, cause: str
) -> None:
    script = _Script()
    root = tmp_path_factory.mktemp("single")

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=root,
            facts=_FACTS,
            responder=script.respond,
            supported_agent_capabilities=_CAPABILITIES,
        )
        run.evaluation.script_benchmark(
            BenchmarkEvaluation(
                executed=False,
                feedback=cause,
                failure_kind=BenchmarkFailureKind.INFRASTRUCTURE,
            ),
            _MEASURED,
        )
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
    # The candidate was never at fault: one paid implementer turn, measured again.
    assert len(implementer) == 1
    assert len(run.evaluation.benchmark_calls) == 2
    assert not any(cause in message for message in implementer)
