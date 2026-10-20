"""The prompt data of the three workstream roles, and the text bounds every prompt shares.

Everything here is a pure function of values the workstream holds; prompts are typed contexts the
renderer turns into text. The only free text is text an agent or an evaluator wrote, cut to a
bound so one long reply cannot crowd out the rest of a prompt.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from vibesys.candidates.api import failure
from vibesys.orchestration.dynamic.search._prompts import (
    EvidenceCitation,
    ImplementPrompt,
    MergePrompt,
    ProfilePrompt,
    ReviewPrompt,
)
from vibesys.orchestration.dynamic.search._values import Blocker, BlockerKind

if TYPE_CHECKING:
    from vibesys.candidates.api import Baseline, ReviewEvaluation
    from vibesys.orchestration.dynamic.search._values import (
        CombineSource,
        MergeSource,
        RoundRecord,
        WorkPlan,
    )
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


class ImplementStart(Protocol):
    """What an implementation request reads of its scheduled workstream.

    It leaves out the input's measurement the workstream started with: the request is given the
    input as known when the turn is asked, so a prompt has one baseline to read.
    """

    @property
    def plan(self) -> WorkPlan: ...
    @property
    def parent(self) -> RevisionRef: ...
    @property
    def prior(self) -> RoundRecord | None: ...
    @property
    def rebase_onto(self) -> RevisionRef | None: ...
    @property
    def combine(self) -> tuple[CombineSource, ...]: ...
    @property
    def parent_main(self) -> RevisionRef | None: ...


_NOT_STUCK = frozenset({BlockerKind.REJECTED, BlockerKind.REPLY_REJECTED})
"""Turns that made a candidate the review or the run refused: the implementer was not stuck."""


def implement_prompt(
    w: ImplementStart, blockers: tuple[Blocker, ...], *, baseline: Baseline | None
) -> ImplementPrompt:
    """One isolated implementation request for the scheduled workstream ``w``.

    ``w.prior`` is the previous round of the same hypothesis, when this workstream continues it;
    ``blockers`` are the turns of this workstream that ended without a measurable candidate;
    ``baseline`` is the input's measurement as known now: None while it has not landed, and an
    unmeasurable one is shown with why it failed.
    ``w.rebase_onto`` is the run's main when it advanced past this hypothesis's work, and
    ``w.parent_main`` the main the work's tree includes: the catch-up applies the change between.
    ``w.combine`` are a combine's parts as scheduled; a part the plan named and scheduling left
    out (no longer offered) is named as missing.
    """
    plan, parent, prior = w.plan, w.parent, w.prior
    latest = blockers[-1] if blockers else None
    stuck = next((item for item in reversed(blockers) if item.kind not in _NOT_STUCK), None)
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
        rebase_onto=w.rebase_onto,
        rebase_from=None if w.rebase_onto is None else w.parent_main,
        combine=w.combine,
        combine_missing=tuple(
            pick
            for pick in plan.combine
            if not any(
                part.hypothesis_id == pick.hypothesis_id
                and part.revision.revision_id.root == pick.revision
                for part in w.combine
            )
        ),
        watch_for=plan.watch_for if plan.combine else "",
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


def merge_prompt(
    plan: WorkPlan, main: RevisionRef, candidates: tuple[MergeSource, ...]
) -> MergePrompt:
    """One merge of the scheduled candidates into ``main``, as the planner instructed it."""
    return MergePrompt(
        merge_id=plan.work_id,
        main_revision=main,
        candidates=candidates,
        instructions=plan.instructions,
        watch_for=plan.watch_for,
    )
