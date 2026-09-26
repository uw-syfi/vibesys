"""Structured response scenario for deterministic VibeSys loop runs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vibesys.schemas import HypothesisOutcome

if TYPE_CHECKING:
    from framework.api import AgentResponseContext


_HYPOTHESIS_ROUNDS = 2
_BASE_METRIC = 1000.0
_METRIC_STEP = 45.0
_METRIC_REGRESSION = 20.0
_METRIC_UNIT = "median_tok_per_sec"
_CLAIMS = (
    "batching the prefill step removes per-request launch overhead",
    "a larger KV cache block trades memory for fewer allocations",
    "reordering the sampler avoids a redundant device sync",
)
_ACTIONS = (
    "batch the prefill step",
    "grow the KV cache block size",
    "reorder the sampler",
)


class VibeSysScriptedResponses:
    """Provide coherent structured answers for built-in loop response schemas."""

    def respond(self, context: AgentResponseContext) -> dict[str, object] | None:
        """Return a schema payload for a known role, or ``None`` if unsupported."""
        if context.output_schema is None:
            return None
        round_index = _round_index(context.round_label, context.turn_number)
        name = context.output_schema.__name__
        hypothesis_id, claim, action = _hypothesis(round_index)
        payloads: dict[str, dict[str, object]] = {
            "PreRoundDecision": {
                "need_profile": False,
                "profile_focus": "",
                "reasoning": "Scripted run skips profiling.",
            },
            "OrchestratorPlan": {
                "hypothesis_id": hypothesis_id,
                "hypothesis": claim,
                "task": action,
                "pass_criteria": "The scripted judge returns a deterministic pass.",
                "reasoning": "Scripted plan.",
                "request_official_evaluation": round_index % _HYPOTHESIS_ROUNDS == 0,
            },
            "ImplementerResponse": {
                "summary": "Scripted implementer completed without workspace changes.",
                "expected_behavior": "The run advances immediately to the judge.",
                "hypothesis_outcome": _outcome(round_index),
                "perf_metric": _metric(round_index),
                "perf_unit": _METRIC_UNIT,
            },
            "JudgeResponse": {
                "analysis": "Scripted judge accepted the invocation.",
                "feedback": "",
                "verdict": "pass",
            },
            "ProfilerSummary": {
                "analysis": "Scripted profile.",
                "bottlenecks": "None; no workload was executed.",
                "suggestions": "None.",
                "perf_metric": _metric(round_index),
                "perf_unit": _METRIC_UNIT,
            },
            "SingleAgentRoundResponse": {
                "summary": "Scripted single-agent round completed.",
                "expected_behavior": "The lifecycle completes immediately.",
                "self_review": "Scripted review passed.",
                "feedback": "",
                "verdict": "pass",
                "bottlenecks": "None.",
                "suggestions": "None.",
                "profile_analysis": "Scripted profile.",
                "perf_metric": _metric(round_index),
                "perf_unit": _METRIC_UNIT,
            },
        }
        return payloads.get(name)


def _round_index(round_label: str | None, turn_number: int) -> int:
    match = re.search(r"(\d+)", round_label or "")
    return int(match.group(1)) if match else turn_number


def _hypothesis(round_index: int) -> tuple[str, str, str]:
    hypothesis_index = (round_index - 1) // _HYPOTHESIS_ROUNDS + 1
    position = (hypothesis_index - 1) % len(_CLAIMS)
    return f"H-{hypothesis_index:02d}", _CLAIMS[position], _ACTIONS[position]


def _metric(round_index: int) -> float:
    hypothesis_index = (round_index - 1) // _HYPOTHESIS_ROUNDS
    value = _BASE_METRIC + hypothesis_index * _METRIC_STEP
    if (hypothesis_index + 1) % 3 == 0:
        return value - _METRIC_REGRESSION
    return value


def _outcome(round_index: int) -> str:
    hypothesis_index = (round_index - 1) // _HYPOTHESIS_ROUNDS + 1
    if round_index % _HYPOTHESIS_ROUNDS != 0:
        return HypothesisOutcome.CONTINUE.value
    if hypothesis_index % 3 == 0:
        return HypothesisOutcome.DISPROVEN.value
    return HypothesisOutcome.SUPPORTED.value
