"""The reuse rule: when an official measurement is answered by a run the scope already made.

An agent measures its candidate through its evaluation tool, and the strategy then asks for
the official measurement of the same candidate. Both run the task's one evaluation on the same
input, so the second run would only repeat the first. Core answers the official measurement
with the earlier run's result instead (`reused_run`) when all of these hold:

- the task opted in (`MeasurementPlan.reuse_agent_run`, declared by the task author and read
  from the project root): an agent that keeps a lucky run and re-measures an unlucky one would
  bias the official score upward by the benchmark's noise, so reuse is off unless the task
  author judges the noise small enough;
- the run belongs to the same scope (owner and generation), so budgets stay per attempt;
- it measured the same input with the same evaluation: its identity equals the requested one
  except for the purpose (`MeasurementIdentity.measures_same_input`), and it ran the same
  stages with the same execution budgets, queue allowance and stage policy;
- both it and the requested measurement get their GPUs and CPU cores to themselves from the
  executor (`MeasurementPlan.dedicated_measurement_resources`, the executor's declared
  capability), so the earlier run was timed as an official run is; a run beside agent work
  never counts;
- it is the scope's only admitted measurement submission of any input, so the official
  score never selects among several runs (the luckiest one, or the one whose input the agent
  kept after measuring others);
- it ended conclusively: terminal, released, accepted, every record trusted and accepted by
  core, and any failure the candidate's own (a workload failure), never the machinery's.

The reused run is a framework-owned trusted job: core admitted it (`SubmitMeasurement`) and
the trusted executor ran it outside agent confinement. Only the agent asked for it. Its own
records stay advice (``local-validation``); the official records are new rows naming it.

A failed run is reused as well as a passed one: reusing only passes would hand a flaky
candidate a second chance that a fresh official run does not get.

The decision is a pure function of core state, taken when the Measure is admitted and recorded
in the submission request (`SubmitMeasurement.reuses`), so a resume replays it. Only the first
submission of an identity can reuse; a retry measures afresh. Records the runtime reports for
a reusing submission must name the run they re-read (`EvidenceRef.reused_from`) and carry that
run's own evidence ids, statuses and mapped kinds (`reused_record`), and once the job is
terminal hold every one of that run's evidence ids (`reused_evidence_complete`), so no reused
record is new evidence and none of the source's records is dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import EvidenceKind, ObservationStatus, RevisionRef
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

__all__ = ["checkpoint_input", "reused_evidence_complete", "reused_record", "reused_run"]

_RUN_PURPOSES = frozenset({"local-validation", "official"})
"""Purposes of runs that measure the task's evaluation on a candidate (not a profile)."""


def _official_kind(source: EvidenceRef) -> EvidenceKind | None:
    """The kind an official record must take when it re-reads ``source``.

    An agent's record is ``local-validation`` whatever stage it read and names the kind of its
    stage (`EvidenceRef.proof_kind`); an official source's record keeps its kind.
    """
    return source.proof_kind if source.kind is EvidenceKind.LOCAL_VALIDATION else source.kind


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
    state: EvaluationState, scope: Scope, plan: MeasurementPlan, identity: MeasurementIdentity
) -> OwnedJob | None:
    """The scope's run that answers the official measurement ``plan`` (of ``identity``), or None.

    None for any other purpose, once the identity has a submission of its own, and whenever
    the scope admitted any other measurement submission, of this input or another.
    """
    if (
        identity.purpose != "official"
        or not plan.reuse_agent_run
        or not plan.dedicated_measurement_resources
        or any(
            budget.scope == scope and budget.identity == identity
            for budget in state.submission_budgets
        )
    ):
        return None
    # Admitted submissions, not accepted jobs: one admitted but not yet accepted is a run too.
    admitted = tuple(
        receipt
        for budget in state.submission_budgets
        if budget.scope == scope and budget.identity.purpose in _RUN_PURPOSES
        for receipt in budget.receipts
    )
    if len(admitted) != 1 or not isinstance(admitted[0], PreparedSubmissionReceipt):
        return None
    source = next(
        (
            job
            for job in state.jobs
            if job.scope == scope and job.submission_id == admitted[0].request_id
        ),
        None,
    )
    if (
        source is None
        or (measured := _identity(source)) is None
        or not measured.measures_same_input(identity)
        or not _same_execution(source.plan, plan)
    ):
        return None
    return source if _conclusive(state, source) else None


def _same_execution(source: MeasurementPlan, requested: MeasurementPlan) -> bool:
    """Whether ``source`` ran as ``requested`` would: same stages, budgets, queue and dedicated resources."""
    return (
        source.reuse_agent_run
        and source.dedicated_measurement_resources
        and source.stages == requested.stages
        and source.queue_allowance == requested.queue_allowance
        and source.policy == requested.policy
    )


def reused_record(
    state: EvaluationState, job: OwnedJob | RegisteredOwnedJob, evidence: EvidenceRef
) -> bool:
    """Whether ``evidence`` may enter the ledger as ``job``'s, as far as reuse is concerned.

    A new run's record names no earlier run. A reusing submission's record names the run it
    re-reads and carries one of that run's evidence ids with its status and artifacts, and
    its kind is exactly the one its source record maps to (an agent's ``local-validation``
    record becomes the correctness or benchmark record its stage names).
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
            and evidence.kind == _official_kind(item)
            for item in source.evidence
        )
        for source in state.jobs
    )


def reused_evidence_complete(
    state: EvaluationState,
    job: OwnedJob | RegisteredOwnedJob,
    accepted: tuple[EvidenceRef, ...],
    *,
    terminal: bool,
) -> bool:
    """Whether a reusing job, once terminal, holds every evidence id of the run it re-reads.

    A reused result is the source's whole result: dropping the record of a failed stage would
    turn a failure into a pass. A job that reuses nothing, or is not yet terminal, is complete.
    """
    reuses = job.reuses if isinstance(job, OwnedJob) else None
    if reuses is None or not terminal:
        return True
    held = {item.evidence_id for item in accepted}
    return any(
        source.submission_id == reuses and {item.evidence_id for item in source.evidence} <= held
        for source in state.jobs
    )
