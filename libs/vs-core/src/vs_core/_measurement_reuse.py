"""The reuse rules: when a measurement is answered by runs the scope already made.

Two measurements are answered from what core holds. The input of the run is measured once
(`held_input_run`): asked for again, as a restarted search does, it is answered with that
run's records, never with a second run and never with a refusal. An official measurement of a
candidate may be answered by the scope's runs of it, as follows.

An agent measures its candidate through its evaluation tool, and the strategy then asks for
the official measurement of the same candidate. Both run the task's one evaluation on the same
input, and a framework run is trusted whoever asked for it, so the official measurement is
answered from the scope's runs of that input instead of repeating them (`reused_run`) when all
of these hold:

- the runs belong to the same scope (owner and generation), so budgets stay per attempt;
- they measured the same input with the same evaluation: the identity equals the requested one
  except for the purpose (`MeasurementIdentity.measures_same_input`), and the run kept the
  same stages with the same execution budgets, queue allowance and stage policy;
- they ended conclusively: terminal, released, accepted, every record trusted and accepted by
  core, and any failure the candidate's own (a workload failure), never the machinery's;
- the samples held have a fresh comparison with the base that is no close call: when they are
  a close call, one more run narrows the noise, and when no base sample is fresh (stale,
  failed or still running) they have no verdict at all (`official_needs_samples`); either
  way the official measurement runs afresh.

Every conclusive run of one measured input is a sample of one measurement
(`vs_core._comparison`). The rule never picks the best of them, and it never refuses because
there are several: the official measurement is answered by *all* of them. The run whose
records carry that answer through the ledger is a failed run if any run failed (a candidate
that failed once did not pass), else the input's first run; the choice depends on no value a
run measured. Its benchmark record enters the ledger stating the mean of the samples
(`vs_core._comparison.as_answer`), the candidate value the comparison states when the answer
is stored, so every reader that ranks candidates reads that one number, and re-running until
a lucky sample gains nothing. (Merge and adoption decisions do not read it yet: they compare
with the input's baseline run, #3136.)

The decision is a pure function of core state, taken when the Measure is admitted and recorded
in the submission request (`SubmitMeasurement.reuses`), so a resume replays it. Only the first
submission of an identity can reuse; a retry measures afresh. Records the runtime reports for
a reusing submission must name the run they re-read (`EvidenceRef.reused_from`) and carry that
run's own evidence ids, kinds, statuses and readings (`reused_record`), and once the job is
terminal hold every one of that run's evidence ids (`reused_evidence_complete`), so no reused
record is new evidence and none of the source's records is dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._comparison import Measured, Moment, evaluation_of, official_needs_samples, run_reading
from .types.common import ObservationStatus, RevisionRef
from .types.evaluation import (
    MEASUREMENT_DIAGNOSTIC_LIMIT,
    MeasurementIdentity,
    MeasurementResult,
    OwnedJob,
    PreparedSubmissionReceipt,
)
from .types.job_observations import MeasurementFailure

if TYPE_CHECKING:
    from .types.common import DecisionId, Scope
    from .types.evaluation import (
        EvaluationState,
        EvidenceRef,
        MeasurementPlan,
        RegisteredOwnedJob,
    )
    from .types.kernel import EvaluationContext

__all__ = [
    "checkpoint_input",
    "held_answer",
    "held_input_run",
    "reused_evidence_complete",
    "reused_record",
    "reused_run",
]

_RUN_PURPOSES = frozenset({"local-validation", "official"})
"""Purposes of runs that measure the task's evaluation on a candidate (not a profile).

The purpose names who asked for the run, not how far it is trusted: every framework run is.
"""

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


def held_input_run(
    state: EvaluationState, scope: Scope, identity: MeasurementIdentity
) -> OwnedJob | None:
    """The run that already measured the input ``identity`` names, once it ended conclusively.

    Only the baseline of a scope is a fact of the input itself: the same measurement asked
    again gives the same answer, so it is read from the run that took it. A run that ended
    short of a conclusion (the machinery failed, or it is still going) answers nothing: the
    budget decides whether the measurement may be run afresh.
    """
    if identity.purpose != "baseline":
        return None
    budget = next(
        (
            held
            for held in state.submission_budgets
            if held.scope == scope and held.identity == identity
        ),
        None,
    )
    latest = None if budget is None or not budget.receipts else budget.receipts[-1]
    if not isinstance(latest, PreparedSubmissionReceipt):
        return None
    run = next((job for job in state.jobs if job.submission_id == latest.request_id), None)
    return run if run is not None and _conclusive(state, run) else None


def held_answer(state: EvaluationState, run: OwnedJob, decision: DecisionId) -> MeasurementResult:
    """What the run that measured an input told, said again to ``decision``.

    The records are the run's own, as its end reported them; what the executor said of a
    failure is kept in the receipt of its submission, and the operator's account of it is
    not kept at all.
    """
    receipts = tuple(
        receipt
        for budget in state.submission_budgets
        for receipt in budget.receipts
        if isinstance(receipt, PreparedSubmissionReceipt)
        and receipt.request_id == run.submission_id
    )
    observation = run.observation
    return MeasurementResult(
        scope=run.scope,
        source_request=run.submission_id,
        decision_id=decision,
        evidence=run.evidence,
        status=run.status,
        failure=receipts[0].failure if receipts else None,
        diagnostic=""
        if observation is None
        else observation.diagnostic[-MEASUREMENT_DIAGNOSTIC_LIMIT:],
    )


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
    state: EvaluationState,
    scope: Scope,
    plan: MeasurementPlan,
    identity: MeasurementIdentity,
    moment: Moment,
) -> OwnedJob | None:
    """The scope's run that carries the answer to the official measurement ``plan``, or None.

    None for any other purpose, once the identity has a submission of its own, when the scope
    holds no conclusive run of this input, and when the samples held leave a close call.
    """
    if identity.purpose != "official" or any(
        budget.scope == scope and budget.identity == identity for budget in state.submission_budgets
    ):
        return None
    sources = tuple(
        job
        for job in state.jobs
        if job.scope == scope
        and job.reuses is None
        and job.plan.purpose in _RUN_PURPOSES
        and (measured := _identity(job)) is not None
        and measured.measures_same_input(identity)
        and _same_execution(job.plan, plan)
        and _conclusive(state, job)
    )
    if not sources:
        return None
    if plan.measured_input is not None and official_needs_samples(
        state, scope, Measured(evaluation_of(identity), plan.measured_input), moment
    ):
        return None
    failed = next((job for job in sources if job.status != ObservationStatus.SUCCEEDED), None)
    return failed if failed is not None else sources[0]


def _same_execution(source: MeasurementPlan, requested: MeasurementPlan) -> bool:
    """Whether ``source`` ran as ``requested`` would: same stages, budgets, queue and policy."""
    return (
        source.stages == requested.stages
        and source.queue_allowance == requested.queue_allowance
        and source.policy == requested.policy
    )


def reused_record(
    state: EvaluationState, job: OwnedJob | RegisteredOwnedJob, evidence: EvidenceRef
) -> bool:
    """Whether ``evidence`` may enter the ledger as ``job``'s, as far as reuse is concerned.

    A new run's record names no earlier run. A reusing submission's record names the run it
    re-reads and carries one of that run's evidence ids with its kind, status, artifacts and the
    reading that run took (`run_reading`: an official run's ledger record states its answer).
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
            and item.kind == evidence.kind
            and run_reading(state, source, item) == evidence.reading
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
