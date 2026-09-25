"""Perf-eval role family: issue_queue's performance evaluator.

The reply's measurement types live in ``vibesys.evaluators.perf_reply``
(they describe measured data, not agent-turn wiring); this module only
declares the ``Role``.
"""

from __future__ import annotations

from vibesys.evaluators.perf_reply import IssuePerfEvalResponse, PerfMetrics
from vibesys.runtime import Reuse, Role, Writes
from vibesys.schemas import PerfTrend


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
    access=Writes(),
    session=Reuse(),
)

ALL_ROLES = (ISSUE_PERF_EVAL,)
