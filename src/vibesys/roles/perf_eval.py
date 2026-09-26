"""Perf-eval role family: issue_queue's performance evaluator.

The reply's measurement types live in ``vibesys.evaluators.perf_reply``
(they describe measured data, not agent-turn wiring); this module only
declares the ``Role``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vibesys.config import LoadLevelCfg
from vibesys.evaluators.perf_reply import IssuePerfEvalResponse, PerfMetrics
from vibesys.runtime import Reuse, Role, Writes
from vibesys.schemas import PerfTrend


class IssuePerfEvalContext(BaseModel):
    """Context for issue_queue's performance-evaluator role (its ``system.j2``)."""

    model_config = ConfigDict(frozen=True)

    load_levels: list[LoadLevelCfg] | None
    perf_metrics_path: str
    issue_create_cap: int
    benchmark_command: str | None
    runtime_notes: str


def _fallback_perf_eval() -> IssuePerfEvalResponse:
    return IssuePerfEvalResponse(
        analysis="No structured response received from perf evaluator.",
        metrics=PerfMetrics(load_levels=[]),
        evaluator_feedback=[],
        new_issue_ids=[],
        throughput_trend=PerfTrend.MIXED,
        latency_trend=PerfTrend.MIXED,
    )


ISSUE_PERF_EVAL = Role(
    id="perf_eval",
    template="loops/issue_queue/perf_eval/system.j2",
    reply=IssuePerfEvalResponse,
    fallback=_fallback_perf_eval,
    context=IssuePerfEvalContext,
    access=Writes(),
    session=Reuse(),
)

ALL_ROLES = (ISSUE_PERF_EVAL,)
