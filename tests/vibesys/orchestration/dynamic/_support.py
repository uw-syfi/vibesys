"""Shared fixtures for the dynamic orchestration tests."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicOptions,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import (
    AgentCapability,
    BenchmarkEvaluation,
    MetricDirection,
    RunFacts,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


def dynamic_options(**changes: object) -> DynamicOptions:
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


def portfolio(
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


def implementation(identifier: str) -> dict[str, object]:
    return {
        "summary": f"Implemented {identifier}.",
        "outcome": "nominated",
        "evidence": [{"location": f"evidence/{identifier}.json", "purpose": "local verification"}],
    }


def requested_slots(message: str) -> int:
    """Return how many workstreams a planning request asks for."""
    return int(message.split("Schedule at most ", 1)[1].split(" ", 1)[0])


class Script:
    def __init__(self, replies: dict[str, list[object]]) -> None:
        self._replies = {role: deque(values) for role, values in replies.items()}
        self.calls: list[tuple[str, str | None, str]] = []
        # Earlier messages of the conversation each call continued.
        self.histories: list[tuple[str, tuple[str, ...]]] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, None, message))
        self.histories.append((role.id, history))
        reply = self._replies[role.id].popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


class JudgeTransportError(RuntimeError):
    """Synthetic agent transport failure for policy recovery tests."""


class EvaluationTransportError(RuntimeError):
    """Synthetic trusted-evaluation failure for task-lifecycle tests."""

    def __init__(self) -> None:
        super().__init__("accuracy transport failed")


INPUT_BASELINE = BenchmarkEvaluation(
    executed=True,
    metric_name="throughput",
    metric_value=1.0,
    metric_direction=MetricDirection.MAXIMIZE,
    row={"throughput": 1.0, "latency": 100.0},
)


def throughput(value: float) -> BenchmarkEvaluation:
    return BenchmarkEvaluation(
        executed=True,
        metric_name="throughput",
        metric_value=value,
        metric_direction=MetricDirection.MAXIMIZE,
        row={"throughput": value},
    )


def input_calls(run: FakeRun) -> int:
    """Return how many benchmarks measured the input (the root workspace)."""
    return sum(call.workspace.id is None for call in run.evaluation.benchmark_calls)


def baseline_run(tmp_path: Path, script: Script) -> FakeRun:
    return FakeRun(
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


def two_epoch_script() -> Script:
    return Script(
        {
            ORCHESTRATOR.id: [portfolio("first"), portfolio("second")],
            IMPLEMENTER.id: [implementation("first"), implementation("second")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )
