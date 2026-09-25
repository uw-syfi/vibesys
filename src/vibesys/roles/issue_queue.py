"""Role catalog for the ``issue_queue`` (``plain``) strategy.

Known gap (see the phase-3a report): ``issue_queue/loop.py`` renders through
``vibesys.prompts.Prompt`` (backend-fragment auto-injection: CUDA/ROCm/CPU
device fragments are added to every render) and renders its system and user
prompts from two separate template files, while ``ctx.agents.turn`` renders
one ``role.template`` through the plain ``render_template`` and takes one
``message`` string. These roles' ``template`` therefore names the *system*
template only; wiring ``issue_queue`` onto ``ctx.agents.turn`` in a later
phase needs either backend-fragment support in ``ctx.agents.turn`` or the
strategy pre-rendering its own user message and passing it as ``message=``
(already representable today).

``issue_id`` is a per-call correlation field on every reply here, so each
role's static ``fallback`` uses a placeholder (``issue_id=0``); a caller
restores the real ID with ``reply.model_copy(update={"issue_id": issue.id})``.
"""

from __future__ import annotations

from vibesys.runtime import Reuse, Role, Writes
from vibesys.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)


def _fallback_implementer() -> IssueImplementerResponse:
    return IssueImplementerResponse(
        issue_id=0,
        summary="Implementer did not produce a structured response.",
        files_touched=[],
        self_check="No structured response received.",
    )


def _fallback_judge() -> IssueJudgeResponse:
    return IssueJudgeResponse(
        issue_id=0,
        analysis="No structured response received from judge.",
        feedback="Judge did not produce a structured response.",
        verdict=Verdict.FAIL,
        new_issues_filed=[],
    )


def _fallback_perf_eval() -> IssuePerfEvalResponse:
    return IssuePerfEvalResponse(
        analysis="No structured response received from perf evaluator.",
        metrics=PerfMetrics(load_levels=[]),
        evaluator_feedback=[],
        new_issue_ids=[],
        throughput_trend=PerfTrend.MIXED,
        latency_trend=PerfTrend.MIXED,
    )


ISSUE_IMPLEMENTER = Role(
    id="implementer",
    template="loops/issue_queue/implementer/system.j2",
    reply=IssueImplementerResponse,
    fallback=_fallback_implementer,
    access=Writes(),
    session=Reuse(),
)

ISSUE_JUDGE = Role(
    id="judge",
    template="loops/issue_queue/judge/system.j2",
    reply=IssueJudgeResponse,
    fallback=_fallback_judge,
    access=Writes(),  # the judge files new issues via its MCP tool grant
    session=Reuse(),
)

ISSUE_PERF_EVAL = Role(
    id="perf_eval",
    template="loops/issue_queue/perf_eval/system.j2",
    reply=IssuePerfEvalResponse,
    fallback=_fallback_perf_eval,
    access=Writes(),
    session=Reuse(),
)

ALL_ROLES = (ISSUE_IMPLEMENTER, ISSUE_JUDGE, ISSUE_PERF_EVAL)
