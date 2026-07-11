"""Fast deterministic agent runner for end-to-end interface smoke tests."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from vibe_serve.agents.progress import AgentProgress

T = TypeVar("T", bound=BaseModel)


class StubAgentRunner:
    """Return valid canned responses without invoking an external agent."""

    backend_name = "stub"

    def invoke(
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str,
        progress: AgentProgress | None = None,
        **kwargs: object,
    ) -> T:
        del workspace, system_prompt, user_prompt, progress, kwargs
        print(f"[stub-agent] {round_label}: starting {kind}")
        time.sleep(0.05)
        response = _response_data(response_cls.__name__)
        print(f"[stub-agent] {round_label}: completed {kind}")
        return response_cls.model_validate(response) if response is not None else fallback_factory()


def _response_data(model_name: str) -> dict[str, object] | None:
    responses: dict[str, dict[str, object]] = {
        "PreRoundDecision": {
            "need_profile": False,
            "profile_focus": "",
            "reasoning": "Stub smoke test skips profiling.",
        },
        "OrchestratorPlan": {
            "task": "Exercise the supervision lifecycle without changing the workspace.",
            "pass_criteria": "The stub judge returns a deterministic pass.",
            "reasoning": "Stub smoke test plan.",
        },
        "ImplementerResponse": {
            "summary": "Stub implementer completed without workspace changes.",
            "expected_behavior": "The run advances immediately to the judge.",
        },
        "JudgeResponse": {
            "analysis": "Stub judge accepted the smoke-test invocation.",
            "feedback": "",
            "verdict": "pass",
        },
        "ProfilerSummary": {
            "analysis": "Stub profile.",
            "bottlenecks": "None; no workload was executed.",
            "suggestions": "None.",
            "perf_metric": None,
            "perf_unit": None,
        },
        "SingleAgentRoundResponse": {
            "summary": "Stub single-agent round completed.",
            "expected_behavior": "The lifecycle completes immediately.",
            "self_review": "Stub review passed.",
            "feedback": "",
            "verdict": "pass",
            "bottlenecks": "None.",
            "suggestions": "None.",
            "profile_analysis": "Stub profile.",
            "perf_metric": None,
            "perf_unit": None,
        },
    }
    return responses.get(model_name)
