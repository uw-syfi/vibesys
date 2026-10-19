"""A measurement submission's conclusion, and recording its observation on its budget receipt.

Split from ``vs_core._measurements`` so each module stays within the file-length ceiling.
``conclusive`` and ``same_conclusion`` are the one definition of when a submission has
ended and when two views of it state the same end. A receipt keeps the submission's latest
observation and the failure class core accepts for it; the helpers here are pure functions
of the committed facts and never import the measurement reducer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import (
    Missing,
    ProofReason,
    Proven,
    Verdict,
    fresh_observation,
    submission_budget_for,
)
from .types.common import ExecuteRegisteredOperation, ObservationStatus
from .types.evaluation import (
    MeasurementSubmissionObserved,
    OwnedJob,
    PreparedSubmissionReceipt,
    SubmissionBudget,
    SubmitMeasurement,
)
from .types.job_observations import MeasurementFailure

if TYPE_CHECKING:
    from .types.common import Observation, RequestId
    from .types.evaluation import EvaluationState, RegisteredOwnedJob
    from .types.kernel import EvaluationContext
    from .types.records import RequestRecord
    from .types.until import FailureReason

__all__ = [
    "canonical_failure",
    "conclusive",
    "ended_unobserved",
    "job_budget",
    "receipt_ended",
    "same_conclusion",
    "submission_receipt",
]


def canonical_failure(
    observation: Observation,
    claim: MeasurementFailure | None,
    *,
    has_facts: bool,
    reuse_incomplete: bool = False,
) -> MeasurementFailure | None:
    """Restrict the caller's failure claim to what the committed facts allow.

    A classification can only narrow what the canonical observation proves. It never
    grants retry authority after execution succeeded, never contradicts committed
    scientific facts, and never exists before a conclusive terminal observation.

    The one claim of its own is a reusing job that ended without all of its source's records
    (``reuse_incomplete``): the result says nothing about the candidate, so it is an
    infrastructure failure whatever the observation's status, and a retry measures afresh.
    """
    if reuse_incomplete and conclusive(observation):
        return MeasurementFailure.INFRASTRUCTURE
    if claim is None or not conclusive(observation):
        return None
    if observation.accepted:
        if observation.status == ObservationStatus.SUCCEEDED:
            return None
        if claim is not None and claim.retryable and has_facts:
            return MeasurementFailure.UNKNOWN
    return claim


def conclusive(observation: Observation | None) -> bool:
    return (
        observation is not None
        and observation.terminal
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def receipt_ended(receipt: PreparedSubmissionReceipt) -> bool:
    """Whether the submission's run is over as far as its receipt says.

    Its observation is terminal, or core ended the submission while no job of it was known
    (``ended_unobserved``). The one definition: a job the run named is the job's to end, and
    whether its run is under way also reads that job (`vs_core._comparison.run_under_way`).
    """
    return receipt.ended_unobserved is not None or (
        receipt.observation is not None and receipt.observation.terminal
    )


def ended_unobserved(
    budget: SubmissionBudget, request_id: RequestId, reason: FailureReason
) -> SubmissionBudget:
    """``budget`` with the receipt of ``request_id`` ended for ``reason``, unless it has ended."""
    return budget.model_copy(
        update={
            "receipts": tuple(
                receipt.model_copy(update={"ended_unobserved": reason})
                if isinstance(receipt, PreparedSubmissionReceipt)
                and receipt.request_id == request_id
                and not receipt_ended(receipt)
                else receipt
                for receipt in budget.receipts
            )
        }
    )


_READ_AGAIN = ("event_id", "sequence", "observed_at")
"""What a later reading of one end changes: which event read it, and when."""


def same_conclusion(job: Observation | None, recorded: Observation | None) -> bool:
    """Whether a job's view of its end states the end that ``recorded`` holds.

    The one comparison of a job's observation with an observation recorded of it elsewhere:
    its submission's ledger row, a release proof, a wake. The ledger takes the job's first
    conclusive observation as its submission's own and keeps it
    (`vs_core._measurements._ended`), while the job takes every later one: a poll that was
    already in flight when the job ended answers again with the same outcome (#3124). Such a
    reading is the same end read later, at a newer sequence, event and time. Any other
    difference, a job view older than the record, or a view that is not a conclusion, is not.
    """
    if job is None or recorded is None:
        return job == recorded
    if job == recorded:
        return True
    if not (conclusive(job) and conclusive(recorded)) or job.sequence <= recorded.sequence:
        return False
    return job.model_copy(update={field: getattr(recorded, field) for field in _READ_AGAIN}) == (
        recorded
    )


def submission_receipt(
    budget: SubmissionBudget,
    event: MeasurementSubmissionObserved,
    source: RequestRecord,
    *,
    reuse_incomplete: bool = False,
) -> Verdict[SubmissionBudget]:
    matches = tuple(
        r
        for r in budget.receipts
        if isinstance(r, PreparedSubmissionReceipt) and r.request_id == event.observation.request_id
    )
    if len(matches) != 1:
        return Missing(ProofReason.ABSENT_RECEIPT)
    receipt = matches[0]
    history = (receipt.observation,) if receipt.observation is not None else ()
    proof = fresh_observation(history, event.observation, complete=True)
    if not isinstance(proof, Proven):
        return proof
    if receipt.observation == event.observation or receipt.failure == MeasurementFailure.WORKLOAD:
        return Missing(ProofReason.UNRESOLVED)
    updated = receipt.model_copy(
        update={
            "observation": event.observation,
            "failure": canonical_failure(
                event.observation,
                event.failure,
                has_facts=source.evaluation_result is not None,
                reuse_incomplete=reuse_incomplete,
            ),
        }
    )
    return Proven(
        budget.model_copy(
            update={"receipts": tuple(updated if r == receipt else r for r in budget.receipts)}
        )
    )


def job_budget(
    state: EvaluationState,
    context: EvaluationContext,
    job: OwnedJob | RegisteredOwnedJob,
    claim: MeasurementFailure | None = None,
    *,
    reuse_incomplete: bool = False,
) -> EvaluationState:
    if job.observation is None:
        return state
    source_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    source = context.records.record(source_id)
    if source is None or not isinstance(
        source.request, SubmitMeasurement | ExecuteRegisteredOperation
    ):
        return state
    proof = submission_budget_for(source.request, state.submission_budgets, context.run.receipts)
    if not isinstance(proof, Proven):
        return state
    receipt = next(
        (
            r
            for r in proof.value.receipts
            if isinstance(r, PreparedSubmissionReceipt) and r.request_id == source_id
        ),
        None,
    )
    updated = submission_receipt(
        proof.value,
        MeasurementSubmissionObserved(
            observation=job.observation,
            failure=(receipt.failure if receipt is not None else None) or claim,
        ),
        source,
        reuse_incomplete=reuse_incomplete,
    )
    return (
        state.model_copy(
            update={
                "submission_budgets": tuple(
                    updated.value if b == proof.value else b for b in state.submission_budgets
                )
            }
        )
        if isinstance(updated, Proven)
        else state
    )
