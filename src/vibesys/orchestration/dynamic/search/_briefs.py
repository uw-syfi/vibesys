"""The prompt data of the three workstream roles, and the text bounds every prompt shares.

Everything here is a pure function of values the workstream holds; prompts are typed contexts the
renderer turns into text. The only free text is text an agent or an evaluator wrote, cut to a
bound so one long reply cannot crowd out the rest of a prompt.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vibesys.candidates.api import failure
from vibesys.orchestration.dynamic.search._prompts import (
    EvidenceCitation,
    ImplementPrompt,
    ProfilePrompt,
    ReviewPrompt,
)
from vibesys.orchestration.dynamic.search._values import Blocker, BlockerKind

if TYPE_CHECKING:
    from vibesys.candidates.api import Baseline, ReviewEvaluation
    from vibesys.orchestration.dynamic.search._values import RoundRecord, WorkPlan
    from vs_core.api.search import RevisionRef

CUT = " [cut]"
FAILURE_CHARS = 1200
"""How much of a failed check's output a prompt shows: its end, where the cause usually is."""
RETRY_TEXT_CHARS = 1200


def bounded(text: str, limit: int) -> str:
    """Cut free agent text to ``limit`` characters for a compact prompt.

    The record keeps the whole text; only the rendered view is bounded, and a cut is marked so
    a reader does not take a prefix for the whole.
    """
    return text if len(text) <= limit else f"{text[:limit]}{CUT}"


def tail(text: str, limit: int) -> str:
    """The last ``limit`` characters of a failure's output, where its cause usually is."""
    return text if len(text) <= limit else f"{CUT.strip()} {text[-limit:]}"


def fence(text: str) -> str:
    """A Markdown code fence that no line of ``text`` can close.

    It is longer than the longest run of backticks in the text, and at least three.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _shown(blocker: Blocker) -> Blocker:
    """The blocker with its agent text cut for the retry prompt."""
    return blocker.model_copy(
        update={
            "summary": bounded(blocker.summary, RETRY_TEXT_CHARS),
            "next_step": bounded(blocker.next_step, RETRY_TEXT_CHARS),
        }
    )


def implement_prompt(
    plan: WorkPlan,
    parent: RevisionRef,
    prior: RoundRecord | None,
    blockers: tuple[Blocker, ...],
    *,
    baseline: Baseline | None,
) -> ImplementPrompt:
    """One isolated implementation request for a scheduled workstream.

    ``prior`` is the previous round of the same hypothesis, when this workstream continues it;
    ``blockers`` are the turns of this workstream that ended without a measurable candidate;
    ``baseline`` is the input's measurement as known now: None while it has not landed, and an
    unmeasurable one is shown with why it failed.
    """
    latest = blockers[-1] if blockers else None
    stuck = next(
        (item for item in reversed(blockers) if item.kind is not BlockerKind.REJECTED), None
    )
    prior_tail = "" if prior is None else tail(prior.failure_tail, FAILURE_CHARS)
    return ImplementPrompt(
        hypothesis_id=plan.work_id,
        hypothesis=plan.hypothesis,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        parent_revision=parent,
        evidence=tuple(EvidenceCitation(location=item) for item in plan.evidence),
        worktree_revision=None if prior is None else prior.candidate,
        prior_revision=None if prior is None else prior.candidate,
        rejection=(
            bounded(latest.summary, RETRY_TEXT_CHARS)
            if latest is not None and latest.kind is BlockerKind.REJECTED
            else None
        ),
        prior_failure_tail=prior_tail,
        baseline_pending=baseline is None,
        input_failure=None if baseline is None else failure(baseline),
        prior_failure_fence=fence(prior_tail),
        last_blocker=None if stuck is None else _shown(stuck),
        turns_without_candidate=len(blockers),
    )


def review_prompt(
    plan: WorkPlan,
    candidate: RevisionRef,
    summary: str,
    evaluations: tuple[ReviewEvaluation, ...],
) -> ReviewPrompt:
    """Independent review of the exact candidate, with every trusted evaluation of it."""
    return ReviewPrompt(
        hypothesis_id=plan.work_id,
        hypothesis=plan.hypothesis,
        pass_criteria=plan.pass_criteria,
        candidate=candidate,
        summary=summary,
        evidence=tuple(EvidenceCitation(location=item) for item in plan.evidence),
        evaluations=evaluations,
    )


def profile_prompt(plan: WorkPlan, target: RevisionRef) -> ProfilePrompt:
    """Measurement-only request against the frozen target revision."""
    return ProfilePrompt(
        question=plan.question, required_fields=plan.required_fields, target=target
    )
