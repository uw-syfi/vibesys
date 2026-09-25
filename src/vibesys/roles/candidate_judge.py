"""Candidate judge role family: evolve's offspring judge.

Evolve reuses the same ``JudgeResponse`` reply schema as ``multi``'s
hypothesis judge (:mod:`vibesys.roles.judge`), but renders its own prompt
template, so it is a distinct ``Role`` in its own module rather than an
addition to ``judge.py`` (keeps that file's diff surface untouched for the
strategies that already own it).
"""

from __future__ import annotations

from vibesys.roles.common import Verdict
from vibesys.roles.judge import JudgeResponse
from vibesys.runtime import ReadOnly, Reuse, Role


def _fallback_candidate_judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="Judge produced no structured response.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
    )


CANDIDATE_JUDGE = Role(
    id="judge",
    template="loops/evolve/judge_prompt.j2",
    reply=JudgeResponse,
    fallback=_fallback_candidate_judge,
    access=ReadOnly(),
    session=Reuse(),
    message="Review the offspring per the criteria above. Return only the JSON verdict.",
)

ALL_ROLES = (CANDIDATE_JUDGE,)
