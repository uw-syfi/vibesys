"""Deterministic multi-policy replies for the product's stub backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vs_loop_state.api import HypothesisOutcome

if TYPE_CHECKING:
    from pydantic import BaseModel

_HYPOTHESIS_ROUNDS = 2
_BASE_METRIC = 1000.0
_METRIC_STEP = 45.0
_METRIC_REGRESSION = 20.0
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


def _hypothesis(round_index: int) -> tuple[str, str, str]:
    index = (round_index - 1) // _HYPOTHESIS_ROUNDS + 1
    position = (index - 1) % len(_CLAIMS)
    return f"H-{index:02d}", _CLAIMS[position], _ACTIONS[position]


def _metric(round_index: int) -> float:
    index = (round_index - 1) // _HYPOTHESIS_ROUNDS
    value = _BASE_METRIC + index * _METRIC_STEP
    return value - _METRIC_REGRESSION if (index + 1) % 3 == 0 else value


def _outcome(round_index: int) -> HypothesisOutcome:
    index = (round_index - 1) // _HYPOTHESIS_ROUNDS + 1
    if round_index % _HYPOTHESIS_ROUNDS != 0:
        return HypothesisOutcome.CONTINUE
    return HypothesisOutcome.DISPROVEN if index % 3 == 0 else HypothesisOutcome.SUPPORTED


def scripted_response(response: type[BaseModel], round_index: int) -> BaseModel | None:
    """Build the multi policy's typed stub response for one round."""
    hypothesis_id, claim, action = _hypothesis(round_index)
    if response is PreRoundDecision:
        return PreRoundDecision(
            need_profile=False,
            profile_focus="",
            reasoning="Scripted run skips profiling.",
        )
    if response is OrchestratorPlan:
        return OrchestratorPlan(
            hypothesis_id=hypothesis_id,
            hypothesis=claim,
            task=action,
            pass_criteria=_ACCEPTANCE_CRITERIA,
            reasoning="Scripted plan.",
            request_official_evaluation=round_index % _HYPOTHESIS_ROUNDS == 0,
        )
    if response is ImplementerResponse:
        return ImplementerResponse(
            summary="Scripted implementer completed without workspace changes.",
            expected_behavior="The run advances immediately to the judge.",
            hypothesis_outcome=_outcome(round_index),
            perf_metric=_metric(round_index),
            perf_unit=_METRIC_UNIT,
        )
    if response is JudgeResponse:
        return JudgeResponse(
            analysis="Scripted judge accepted the invocation.",
            feedback="",
            verdict=Verdict.PASS,
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
