"""Deterministic single-policy replies for the product's stub backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.models import SingleAgentRoundResponse

if TYPE_CHECKING:
    from pydantic import BaseModel

_BASE_METRIC = 1000.0
_HYPOTHESIS_ROUNDS = 2
_METRIC_REGRESSION = 20.0
_METRIC_STEP = 45.0
_METRIC_UNIT = "median_tok_per_sec"
_ACCEPTANCE_CRITERIA = "The scripted judge returns a deterministic pass."
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


def _metric(round_index: int) -> float:
    index = (round_index - 1) // _HYPOTHESIS_ROUNDS
    value = _BASE_METRIC + index * _METRIC_STEP
    return value - _METRIC_REGRESSION if (index + 1) % 3 == 0 else value


def scripted_response(response: type[BaseModel], round_index: int) -> BaseModel | None:
    """Build the single policy's typed stub response for one round."""
    hypothesis_index = (round_index - 1) // _HYPOTHESIS_ROUNDS + 1
    hypothesis_id = f"H-{hypothesis_index:02d}"
    position = (hypothesis_index - 1) % len(_CLAIMS)
    if response is OrchestratorPlan:
        return OrchestratorPlan(
            hypothesis_id=hypothesis_id,
            hypothesis=_CLAIMS[position],
            task=_ACTIONS[position],
            pass_criteria=_ACCEPTANCE_CRITERIA,
            reasoning="Scripted plan.",
            request_official_evaluation=round_index % _HYPOTHESIS_ROUNDS == 0,
        )
    if response is SingleAgentRoundResponse:
        return SingleAgentRoundResponse(
            summary="Scripted single-agent round completed.",
            expected_behavior="The lifecycle completes immediately.",
            self_review="Scripted review passed.",
            feedback="",
            verdict=Verdict.PASS,
            bottlenecks="None.",
            suggestions="None.",
            profile_analysis="Scripted profile.",
            perf_metric=_metric(round_index),
            perf_unit=_METRIC_UNIT,
        )
    if response is ProfilerSummary:
        return ProfilerSummary(
            analysis="Scripted profile.",
            bottlenecks="None; no workload was executed.",
            suggestions="None.",
            perf_metric=_metric(round_index),
            perf_unit=_METRIC_UNIT,
        )
    return None


__all__ = ["scripted_response"]
