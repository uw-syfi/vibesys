"""The labels the issue-queue search gives its spans, in the shared vocabulary (``search_spans``).

Every job is a workstream of the round it belongs to: an issue job is named for its issue, its
idea is the issue's title, and it passed (naming the revision it kept) when it closed the
issue with a change (failed otherwise); an evaluation is named for its iteration and only
looks (a probe), so it reaches no verdict. An issue job is linked to the job
that kept the tip it builds on. Its turns are agent turns (``turn_label``) and the accuracy
proof of the final candidate is the run's one gate (`_proof`). Every label goes through
``ctx.label``; nothing here builds a projection.
"""

from vibesys.orchestration.issue_queue.search._values import Closing, IssueJob, PerfJob
from vibesys.search_spans.api import BUILDS_ON, FAILED, PASSED, WORKSTREAM, WorkstreamAttrs
from vs_core.api.search import Flow, RevisionRef, SpanId, SpanLink

NO_CHANGE = "no_change"
"""Why a job that closed its issue without changing anything failed."""
NOT_CLOSED = "not_closed"
"""Why a job whose issue stayed open (its attempts were spent) failed."""


def _issue_attrs(job: IssueJob, kept: RevisionRef | None = None) -> WorkstreamAttrs:
    """An issue job's issue, the tip it builds on and, once passed, the revision it kept."""
    return WorkstreamAttrs(
        hypothesis_id=f"issue-{job.issue.id}",
        parent_revision=job.base.revision_id.root,
        round=job.iteration,
        idea=job.issue.title or None,
        revision=None if kept is None else kept.revision_id.root,
    )


def issue_begun(ctx: Flow, job: IssueJob) -> None:
    """Label the issue job's span a workstream named for its issue."""
    ctx.label(WORKSTREAM, title=f"#{job.issue.id} {job.issue.title}", attrs=_issue_attrs(job))


def evaluation_begun(ctx: Flow, job: PerfJob) -> None:
    """Label the evaluation's span a workstream named for its iteration."""
    attrs = WorkstreamAttrs(
        hypothesis_id=f"evaluation-{job.iteration}",
        parent_revision=job.base.revision_id.root,
        round=job.iteration,
        probe=True,
    )
    ctx.label(WORKSTREAM, title=f"performance evaluation {job.iteration}", attrs=attrs)


def issue_settled(ctx: Flow, job: IssueJob, closing: Closing, *, retained: bool) -> None:
    """Accept the issue job's workstream when its workspace kept a candidate, else reject it."""
    if retained:
        ctx.label(WORKSTREAM, attrs=_issue_attrs(job, closing.candidate), verdict=PASSED)
        return
    if closing.failure is not None:
        reason = closing.failure.kind.value
    else:
        reason = NO_CHANGE if closing.closed else NOT_CLOSED
    ctx.label(WORKSTREAM, verdict=FAILED, reason=reason)


def issue_not_opened(ctx: Flow, closing: Closing) -> None:
    """Reject the issue job's workstream: its workspace never opened."""
    reason = None if closing.failure is None else closing.failure.kind.value
    ctx.label(WORKSTREAM, verdict=FAILED, reason=reason)


def builds_on(ctx: Flow, job: SpanId | None, tip: SpanId | None) -> None:
    """Link the workstream ``job`` to the one that kept the tip it builds on."""
    if job is not None and tip is not None:
        ctx.label(WORKSTREAM, span=job, links=(SpanLink(rel=BUILDS_ON, to=tip),))
