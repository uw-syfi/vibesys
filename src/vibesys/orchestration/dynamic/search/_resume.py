"""Where a workstream resumes after a code change: the last thing it established in its workspace.

A workstream checkpoints its start, and then, once its workspace is open, each thing it
establishes that is costly to redo (`WorkstreamResume`): the implementer's turns so far, a
candidate before its review, the candidate to measure, its trusted measurement, and what the
close asserts. A restart after a code change resumes it from the latest of these in the same
workspace (`ctx.workspaces.reopen`), so a finished candidate is never made or measured again.

Each checkpoint that names a candidate also records how its workspace settles if the restart
cannot give it back (`ctx.workspaces.salvage`): as work in progress, unmeasured, so its code is
kept. A resumed workstream that cannot reopen its workspace ends unmeasured
(`WorkstreamDone.lost`).
"""

from enum import StrEnum
from typing import Literal

from vibesys.candidates.api import Baseline
from vibesys.orchestration.dynamic.search._outcomes import (
    Closing,
    MergeReport,
    Result,
    Work,
    WorkstreamStart,
    salvage_of,
)
from vibesys.orchestration.dynamic.search._values import Blocker
from vs_core.api.search import (
    Assessment,
    AttemptRef,
    Flow,
    MeasurementResult,
    Value,
)


class ResumeStage(StrEnum):
    """What the workstream had established in its workspace when it checkpointed. Closed."""

    WORKING = "working"
    """The implementer's turns are under way: ``blockers`` say why the earlier ones gave no
    candidate (none yet when the workspace has just opened)."""
    IMPLEMENTED = "implemented"
    """The implementer made ``result``, which is to be reviewed."""
    READY = "ready"
    """``result`` is the candidate to measure (a profile's has none: its parent is measured)."""
    MEASURED = "measured"
    """``measurement`` is the candidate's trusted measurement, to interpret."""
    SETTLING = "settling"
    """``closing`` is what the close of the workspace asserts."""


class WorkstreamResume(Value):
    """A workstream's checkpoint inside its workspace: where it resumes after a code change.

    ``version`` is this model's own: a change an older checkpoint does not decode as is a new
    version, and the workstream takes the union of the versions it can resume.
    """

    kind: Literal["workstream-resume"] = "workstream-resume"
    version: Literal[1] = 1
    start: WorkstreamStart
    workspace: AttemptRef
    stage: ResumeStage
    blockers: tuple[Blocker, ...] = ()
    carried: Result | None = None
    """The latest candidate the implementer made, reviewed or not (`Work.carried`)."""
    merged: MergeReport | None = None
    """The merger's commit, once a merge made one (`Work.merged`)."""
    result: Result | None = None
    measurement: MeasurementResult | None = None
    closing: Closing | None = None
    baseline: Baseline | None = None
    input_wait_since: float | None = None
    overdue_reported: bool = False

    def work(self) -> Work:
        """What the workstream held when it checkpointed, but its workspace (reopened apart)."""
        return Work(
            carried=self.carried,
            merged=self.merged,
            baseline=self.baseline,
            input_wait_since=self.input_wait_since,
            overdue_reported=self.overdue_reported,
        )

    def at(self, *stages: ResumeStage) -> bool:
        """Whether the workstream checkpointed at one of ``stages``."""
        return self.stage in stages


def mark(  # noqa: PLR0913  # lint-waiver: LW-271016 [PLR0913]; each keyword is one stage's own fact, set only at that stage.
    # > A per-stage record type would duplicate `WorkstreamResume` field by field, and one
    # > `WorkstreamResume` argument would make every caller restate the work it already holds.
    ctx: Flow,
    w: WorkstreamStart,
    work: Work,
    stage: ResumeStage,
    *,
    blockers: tuple[Blocker, ...] = (),
    result: Result | None = None,
    measurement: MeasurementResult | None = None,
    closing: Closing | None = None,
) -> None:
    """Checkpoint what the workstream established at ``stage`` in its open workspace.

    The latest candidate it names, or the merger's commit, is salvaged: kept as work in progress
    if a restart cannot give the workspace back.
    """
    workspace = work.workspace
    if workspace is None:
        message = "a workstream checkpoints its progress only in an open workspace"
        raise ValueError(message)
    ctx.checkpoint(
        WorkstreamResume(
            start=w,
            workspace=workspace.ref,
            stage=stage,
            blockers=blockers,
            carried=work.carried,
            merged=work.merged,
            result=result,
            measurement=measurement,
            closing=closing,
            baseline=work.baseline,
            input_wait_since=work.input_wait_since,
            overdue_reported=work.overdue_reported,
        )
    )
    candidate = result or work.carried
    closing = Closing(result=candidate, merge=work.merged)
    revision = closing.candidate  # a merger's commit, else the implementer's latest
    if revision is not None:
        salvage = salvage_of(closing, ctx.task.terms)
        ctx.workspaces.salvage(
            workspace,
            revision,
            Assessment(
                outcome=salvage.outcome,
                assessments=salvage.assessments,
                eligible=salvage.eligible,
                retention=salvage.retention,
            ),
        )
