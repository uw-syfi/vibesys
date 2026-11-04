"""The durable record a Slurm evaluation executor keeps for each evaluation it owns.

The record is written before every boundary the evaluation crosses (admission,
dispatch, the scheduler's acceptance, a terminal outcome), so a restarted executor
resumes, reconciles or replays the evaluation from it alone.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from vs_evaluation.api import EvaluationRequest
from vs_slurm.api import SlurmBatchHandle


class DurableEnd(StrEnum):
    """How an evaluation ended without a scheduler job."""

    CANCELLED_BEFORE_DISPATCH = "cancelled_before_dispatch"
    # Dispatch started, but its sbatch never reached the scheduler: cancellation
    # acknowledgements with no job, repeated, proved no job exists.
    CANCELLED_WITHOUT_JOB = "cancelled_without_job"
    # A ledger evaluation this run let go of (#2757): its job belongs to the
    # evaluation ledger, which keeps it for other runs or cancels it once no run
    # has leased it for the grace period. This run reports it cancelled.
    RELEASED = "released"


class DurableSlurmEvaluation(BaseModel):
    """The saved record: the only lifecycle state that survives a restart.

    Every read path (poll, inspect, recovery, cancel) decides from this record, so a
    fact that must outlive the process is written here before it is published.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: SlurmBatchHandle | None
    request: EvaluationRequest
    # The deadlines of the job's current scheduler attempt (``slurm_deadlines``):
    # ``wait_deadline_epoch_s`` bounds its queue wait (None: unbounded),
    # ``run_deadline_epoch_s`` its run from when it was first seen running.
    # ``deadline_attempt`` is None until the deadlines were first saved; a record
    # from before the split has only ``wait_deadline_epoch_s``, read as attempt 0.
    wait_deadline_epoch_s: float | None = None
    run_deadline_epoch_s: float | None = None
    deadline_attempt: int | None = None
    # Legacy records always contained an accepted handle. Missing dispatch
    # evidence is conservative: never authorize a replay from its absence.
    dispatch_started: bool = True
    submission_rejection: str | None = None
    # The rejection came from a run environment that can run no Slurm work (root probe).
    run_environment_unavailable: bool = False
    # A provider failure is final only after cleanup proves no allocation remains.
    # Persist it before process-local publication so polling-only recovery sees the
    # same terminal result instead of reconciling an already-closed dispatch forever.
    terminal_failure: str | None = None
    # Set once the evaluation was cancelled while it waited for local admission.
    # Such a record is final: nothing may dispatch it.
    ended: DurableEnd | None = None
    # The evaluation ledger key of the request this evaluation attached to, when
    # the operator enabled the ledger. The workspace changes once artifacts land,
    # so the key is saved, not recomputed.
    ledger_key: str | None = None

    @model_validator(mode="after")
    def _ended_before_dispatch_has_no_job(self) -> DurableSlurmEvaluation:
        if self.ended is DurableEnd.RELEASED and self.ledger_key is None:
            message = "only a ledger evaluation can be released"
            raise ValueError(message)
        if self.ended not in {None, DurableEnd.RELEASED} and self.handle is not None:
            message = "an evaluation that ended without a job cannot have a job"
            raise ValueError(message)
        if self.ended is DurableEnd.CANCELLED_BEFORE_DISPATCH and self.dispatch_started:
            message = "an evaluation ended before dispatch cannot have a dispatch"
            raise ValueError(message)
        terminal_facts = sum(
            value is not None
            for value in (self.submission_rejection, self.terminal_failure, self.ended)
        )
        if terminal_facts > 1:
            message = "an evaluation cannot have multiple durable terminal outcomes"
            raise ValueError(message)
        if self.run_environment_unavailable and self.submission_rejection is None:
            message = "only a rejected evaluation can say the run environment is unavailable"
            raise ValueError(message)
        return self
