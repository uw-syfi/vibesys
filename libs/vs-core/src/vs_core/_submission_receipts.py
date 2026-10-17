"""Recording a measurement submission's observation on its budget receipt.

Split from ``vs_core._measurements`` so each module stays within the file-length ceiling.
A receipt keeps the submission's latest observation and the failure class core accepts for
it; the helpers here are pure functions of the committed facts and never import the
measurement reducer.
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
    from .types.common import Observation
    from .types.evaluation import EvaluationState, RegisteredOwnedJob
    from .types.kernel import EvaluationContext
    from .types.records import RequestRecord

__all__ = ["canonical_failure", "conclusive", "job_budget", "submission_receipt"]


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
