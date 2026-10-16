"""The reuse rule: when an official measurement is answered by a run the scope already made.

An agent measures its candidate through its evaluation tool, and the strategy then asks for
the official measurement of the same candidate. Both run the task's one evaluation on the same
input, so the second run would only repeat the first. Core answers the official measurement
with the earlier run's result instead (`reused_run`) when all of these hold:

- the run belongs to the same scope (owner and generation), so budgets stay per attempt;
- it measured the same input with the same evaluation: its identity equals the requested one
  except for the purpose (`MeasurementIdentity.measures_same_input`);
- it is the only run of that input in the scope, and the scope's latest run, so an agent
  cannot keep the luckiest of several runs of one input, nor return to an older input after
  measuring another;
- it ended conclusively: terminal, released, accepted, every record trusted and accepted by
  core, and any failure the candidate's own (a workload failure), never the machinery's.

A failed run is reused as well as a passed one: reusing only passes would hand a flaky
candidate a second chance that a fresh official run does not get.

The decision is a pure function of core state, taken when the Measure is admitted and recorded
in the submission request (`SubmitMeasurement.reuses`), so a resume replays it. Only the first
submission of an identity can reuse; a retry measures afresh. Records the runtime reports for
a reusing submission must name the run they re-read (`EvidenceRef.reused_from`) and carry that
run's own evidence ids and statuses (`reused_record`), so no reused record is new evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import ObservationStatus, RevisionRef
from .types.evaluation import MeasurementIdentity, OwnedJob, PreparedSubmissionReceipt
from .types.job_observations import MeasurementFailure

if TYPE_CHECKING:
    from .types.common import Scope
    from .types.evaluation import (
        EvaluationState,
        EvidenceRef,
        MeasurementPlan,
        RegisteredOwnedJob,
    )
    from .types.kernel import EvaluationContext

__all__ = ["checkpoint_input", "reused_record", "reused_run"]

_RUN_PURPOSES = frozenset({"local-validation", "official"})
"""Purposes of runs that measure the task's evaluation on a candidate (not a profile)."""

_CONCLUSIVE = frozenset({ObservationStatus.SUCCEEDED, ObservationStatus.FAILED})


def checkpoint_input(
    context: EvaluationContext, scope: Scope, plan: MeasurementPlan
) -> MeasurementPlan:
    """``plan`` with the measured input of its candidate, from the scope's first checkpoint of it.

    Checkpoints are only appended, so the first one naming the revision answers the same on
    every replay. A plan that already names its input (an agent's) is kept as it is.
    """
    if plan.measured_input is not None or not isinstance(plan.candidate, RevisionRef):
        return plan
    first = next(
        (
            checkpoint
            for owner in context.attempts.attempts
            if owner.attempt_id == scope.owner and owner.generation == scope.generation
            for checkpoint in owner.checkpoints
            if checkpoint.revision == plan.candidate
        ),
        None,
    )
    if first is None or first.measured_input is None:
        return plan
    return plan.model_copy(update={"measured_input": first.measured_input})


def _identity(job: OwnedJob) -> MeasurementIdentity | None:
    candidate = job.plan.candidate
    return (
        MeasurementIdentity.from_plan(job.plan, candidate)
        if isinstance(candidate, RevisionRef)
        else None
    )


def _conclusive(state: EvaluationState, job: OwnedJob) -> bool:
    receipts = tuple(
        receipt
        for budget in state.submission_budgets
        for receipt in budget.receipts
        if isinstance(receipt, PreparedSubmissionReceipt)
        and receipt.request_id == job.submission_id
    )
    failure = receipts[0].failure if len(receipts) == 1 else None
    return (
        len(receipts) == 1
        and failure in (None, MeasurementFailure.WORKLOAD)
        and job.terminal
        and job.released
        and job.status in _CONCLUSIVE
        and job.observation is not None
        and job.observation.accepted
        and (job.status != ObservationStatus.SUCCEEDED or bool(job.evidence))
        and all(
            item.provenance == "trusted"
            and item.acceptance_receipt is not None
            and item.status in _CONCLUSIVE
            for item in job.evidence
        )
    )


def reused_run(
    state: EvaluationState, scope: Scope, identity: MeasurementIdentity
) -> OwnedJob | None:
    """The scope's run that answers an official measurement of ``identity``, or None.

    None for any other purpose, and once the identity has a submission of its own.
    """
    if identity.purpose != "official" or any(
        budget.scope == scope and budget.identity == identity for budget in state.submission_budgets
    ):
        return None
    runs = tuple(
        job
        for job in state.jobs
        if job.scope == scope and job.reuses is None and job.plan.purpose in _RUN_PURPOSES
    )
    same = tuple(
        job
        for job in runs
        if (measured := _identity(job)) is not None and measured.measures_same_input(identity)
    )
    if len(same) != 1 or same[0] != runs[-1]:
        return None
    return same[0] if _conclusive(state, same[0]) else None


def reused_record(
    state: EvaluationState, job: OwnedJob | RegisteredOwnedJob, evidence: EvidenceRef
) -> bool:
    """Whether ``evidence`` may enter the ledger as ``job``'s, as far as reuse is concerned.

    A new run's record names no earlier run. A reusing submission's record names the run it
    re-reads and carries one of that run's evidence ids with its status and artifacts.
    """
    reuses = job.reuses if isinstance(job, OwnedJob) else None
    if evidence.reused_from != reuses:
        return False
    if reuses is None:
        return True
    return any(
        source.submission_id == reuses
        and any(
            item.evidence_id == evidence.evidence_id
            and item.status == evidence.status
            and item.artifacts == evidence.artifacts
            for item in source.evidence
        )
        for source in state.jobs
    )
