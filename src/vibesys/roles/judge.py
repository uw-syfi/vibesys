"""Judge role family: hypothesis judge (multi) and issue judge (issue_queue).

``issue_id`` is a per-call correlation field on the issue judge's reply, so
its ``fallback`` uses a placeholder (``issue_id=0``); a caller restores the
real ID with ``reply.model_copy(update={"issue_id": issue.id})``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from vibesys.roles.common import SkillResourceSelection, Verdict
from vibesys.runtime import Fresh, ReadOnly, Reuse, Role, Writes


class JudgeResponse(BaseModel):
    """Structured response from the hypothesis judge agent (multi)."""

    analysis: str = Field(
        description="Detailed analysis of the implementation covering correctness, completeness, dependencies, tests, and code quality."
    )
    feedback: str = Field(
        description="Specific actionable feedback for the implementer. Empty string if passing."
    )
    verdict: Verdict = Field(description="PASS if all criteria are met, FAIL otherwise.")
    skills_used: list[SkillResourceSelection] = Field(
        default_factory=list,
        description=(
            "Skill resources independently selected for this review; observational "
            "only and never inherited by the implementer."
        ),
    )


class IssueJudgeResponse(BaseModel):
    """Structured response from the issue judge agent in the plain loop.

    The judge evaluates whether the current issue was sufficiently
    resolved while also retaining the basic correctness/test checks of
    the cross-loop judge.
    """

    issue_id: int = Field(description="ID of the issue under review.")
    analysis: str = Field(
        description="Detailed analysis covering whether the issue is resolved AND general correctness checks."
    )
    feedback: str = Field(
        description="Specific actionable feedback for the implementer if not resolved. Empty if PASS."
    )
    verdict: Verdict = Field(
        description="PASS if the issue is resolved AND general checks pass, FAIL otherwise."
    )
    new_issues_filed: list[int] = Field(
        default_factory=list,
        description="IDs of new bug-type issues the judge filed via create_issue for unrelated discoveries.",
    )


def _fallback_hypothesis_judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="Judge produced no structured response.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
    )


def _fallback_issue_judge() -> IssueJudgeResponse:
    return IssueJudgeResponse(
        issue_id=0,
        analysis="No structured response received from judge.",
        feedback="Judge did not produce a structured response.",
        verdict=Verdict.FAIL,
        new_issues_filed=[],
    )


MULTI_JUDGE = Role(
    id="judge",
    template="loops/multi/judge_prompt.j2",
    reply=JudgeResponse,
    fallback=_fallback_hypothesis_judge,
    access=ReadOnly(),
    session=Fresh(),
    filter_skills=True,
    message="Review the implementation per the criteria above. Return only the JSON verdict.",
)

ISSUE_JUDGE = Role(
    id="judge",
    template="loops/issue_queue/judge/system.j2",
    reply=IssueJudgeResponse,
    fallback=_fallback_issue_judge,
    access=Writes(),  # the judge files new issues via its MCP tool grant
    session=Reuse(),
)

ALL_ROLES = (MULTI_JUDGE, ISSUE_JUDGE)
