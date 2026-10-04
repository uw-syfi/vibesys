"""Produce history only from durable, owner-normalized measurement facts.

The submission order is the canonical outbox order. Missing legacy submissions,
historical budget receipts, or scientific outcomes leave coverage unavailable.
Consumers may capture a cursor only from COMPLETE coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .types.common import (
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    RevisionRef,
)
from .types.evaluation import (
    MeasurementIdentity,
    PreparedSubmissionReceipt,
    SubmitMeasurement,
)
from .types.evaluation_history import (
    AttemptEvaluationHistory,
    AttemptEvaluationRecord,
    EvaluationHistoryAvailability,
)
from .types.strategy import Accepted, Operation, StartAttempt

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import Scope
    from .types.evaluation import EvaluationState
    from .types.intents import Intent, IntentsState
    from .types.kernel import RunState

__all__ = ["produce_history"]


class _VerdictTruthError(TypeError):
    def __init__(self) -> None:
        super().__init__("history proof requires explicit Proven/Missing/Mismatch matching")


class _Verdict:
    def __bool__(self) -> bool:
        raise _VerdictTruthError


@dataclass(frozen=True)
class Proven[T](_Verdict):
    """An exact leaf-owned submission fact, never a phase-derived assumption."""

    fact: T


@dataclass(frozen=True)
class Missing(_Verdict):
    """Absent fact cannot contribute a durable history record."""

    reason: Literal["submission", "budget", "normalization"]


@dataclass(frozen=True)
class Mismatch(_Verdict):
    """Present conflicting fact cannot contribute a durable history record."""

    field: Literal["payload", "scope", "observation", "identity", "declaration"]


type Verdict[T] = Proven[T] | Missing | Mismatch


def produce_history(
    scope: Scope,
    evaluation: EvaluationState,
    intents: IntentsState,
    owner: AttemptView,
    run: RunState,
) -> AttemptEvaluationHistory:
    """Certify exact submission coverage, retaining unavailable partial history."""
    submissions = tuple(
        row
        for row in intents.intents
        if row.request.scope == scope
        and (
            isinstance(row.request, SubmitMeasurement)
            or (
                isinstance(row.request, ExecuteRegisteredOperation)
                and row.lifecycle == LifecycleClass.OWNED_JOB
                and any(
                    job.request_id == row.request_id and job.expected_measurement is not None
                    for job in evaluation.registered_jobs
                )
            )
        )
    )
    ids = tuple(row.request_id for row in submissions)
    expected = {job.submission_id for job in evaluation.jobs if job.scope == scope} | {
        job.request_id
        for job in evaluation.registered_jobs
        if job.scope == scope and job.expected_measurement is not None
    }
    receipts = tuple(
        receipt
        for budget in evaluation.submission_budgets
        if budget.scope == scope
        for receipt in budget.receipts
    )
    expected.update(
        receipt.request_id for receipt in receipts if isinstance(receipt, PreparedSubmissionReceipt)
    )
    records = []
    for ordinal, row in enumerate(submissions, 1):
        observation = row.observation
        proof = _submission_identity(row, evaluation, run)
        identity = proof.fact if isinstance(proof, Proven) else None
        if (
            identity is None
            or not isinstance(_budgeted(row, identity, evaluation), Proven)
            or (
                row.evaluation_result is not None
                and any(
                    stage.stage_id not in {item.stage_id for item in identity.stages}
                    for stage in row.evaluation_result.stages
                )
            )
            or observation is None
            or observation.request_id != row.request_id
            or observation.scope != scope
            or not observation.terminal
            or observation.status in (ObservationStatus.PENDING, ObservationStatus.UNKNOWN)
            or (observation.accepted and row.evaluation_result is None)
        ):
            continue
        facts = row.evaluation_result.model_dump() if row.evaluation_result is not None else {}
        records.append(
            AttemptEvaluationRecord(
                ordinal=ordinal,
                submission_id=row.request_id,
                scope=scope,
                terminal_observation=observation,
                **facts,
            )
        )
    complete = (
        expected <= set(ids)
        and len(set(ids)) == len(ids)
        and len(records) == len(ids)
        and all(isinstance(receipt, PreparedSubmissionReceipt) for receipt in receipts)
        and (bool(ids) or _certified_empty(owner, run))
    )
    return AttemptEvaluationHistory(
        availability=(
            EvaluationHistoryAvailability.COMPLETE
            if complete
            else EvaluationHistoryAvailability.UNAVAILABLE
        ),
        covered_submissions=ids,
        records=tuple(records),
    )


def _budgeted(
    row: Intent, identity: MeasurementIdentity, evaluation: EvaluationState
) -> Verdict[PreparedSubmissionReceipt]:
    budgets = tuple(
        budget
        for budget in evaluation.submission_budgets
        if budget.scope == row.request.scope and budget.identity == identity
    )
    if not budgets:
        return Missing("budget")
    if len(budgets) != 1:
        return Mismatch("identity")
    matches = tuple(
        receipt
        for budget in budgets
        for receipt in budget.receipts
        if isinstance(receipt, PreparedSubmissionReceipt) and receipt.request_id == row.request_id
    )
    if not matches:
        return Missing("submission")
    return Proven(matches[0]) if len(matches) == 1 else Mismatch("identity")


def _submission_identity(
    row: Intent, evaluation: EvaluationState, run: RunState
) -> Verdict[MeasurementIdentity]:
    request = row.request
    if request.request_id != row.request_id:
        return Mismatch("identity")
    observation = row.observation
    if observation is not None and (
        observation.request_id != row.request_id
        or observation.scope != request.scope
        or observation.admission_id != request.admission_id
    ):
        return Mismatch("observation")
    if row.lifecycle != LifecycleClass.OWNED_JOB:
        return Mismatch("declaration")
    if isinstance(request, SubmitMeasurement):
        return _builtin_identity(row, request, evaluation)
    if not isinstance(request, ExecuteRegisteredOperation):
        return Mismatch("declaration")
    return _registered_identity(row, request, evaluation, run)


def _builtin_identity(
    row: Intent, request: SubmitMeasurement, evaluation: EvaluationState
) -> Verdict[MeasurementIdentity]:
    jobs = tuple(job for job in evaluation.jobs if job.submission_id == row.request_id)
    if (
        len(jobs) != 1
        or jobs[0].scope != request.scope
        or jobs[0].plan != request.plan
        or jobs[0].observation != row.observation
        or (
            row.observation is not None
            and row.observation.accepted
            and row.observation.resource_id != jobs[0].resource_id
        )
    ):
        return Mismatch("payload") if jobs else Missing("submission")
    plan = request.plan
    candidate = plan.candidate
    if not isinstance(candidate, RevisionRef):
        return Missing("normalization")
    return Proven(MeasurementIdentity.from_plan(plan, candidate))


def _registered_identity(
    row: Intent,
    request: ExecuteRegisteredOperation,
    evaluation: EvaluationState,
    run: RunState,
) -> Verdict[MeasurementIdentity]:
    schema = request.operation.schema_ref
    descriptors = tuple(
        descriptor for descriptor in run.capabilities.operations if descriptor.kind == schema.kind
    )
    if len(descriptors) != 1 or (
        descriptors[0].request_schema != schema.request_schema
        or descriptors[0].outcome_schema != schema.outcome_schema
        or descriptors[0].lifecycle != schema.lifecycle
        or schema.lifecycle != LifecycleClass.OWNED_JOB
    ):
        return Mismatch("declaration")
    jobs = tuple(job for job in evaluation.registered_jobs if job.request_id == row.request_id)
    if (
        len(jobs) != 1
        or jobs[0].operation_id != request.operation_id
        or jobs[0].scope != request.scope
        or jobs[0].observation != row.observation
        or (
            row.observation is not None
            and row.observation.accepted
            and row.observation.resource_id != jobs[0].resource_id
        )
    ):
        return Mismatch("observation") if jobs else Missing("submission")
    origins = tuple(
        receipt for receipt in run.receipts if receipt.decision_id == request.decision_id
    )
    if not origins:
        return Missing("submission")
    if len(origins) != 1:
        return Mismatch("identity")
    receipt = origins[0]
    decision = receipt.decision
    if (
        isinstance(receipt.feedback, Accepted)
        and isinstance(decision, Operation)
        and receipt.decision_id == decision.decision_id
        and receipt.feedback.decision_id == receipt.decision_id
        and row.request_id in receipt.request_ids
        and decision.scope == request.scope
        and decision.deadline_at == request.deadline_at
        and decision.registered_wire == request.operation
        and decision.registered_measurement is not None
        and decision.registered_measurement == decision.normalized_measurement
        and decision.normalized_measurement == jobs[0].expected_measurement
    ):
        return Proven(decision.registered_measurement)
    return Mismatch("declaration")


def _certified_empty(owner: AttemptView, run: RunState) -> bool:
    previous = owner.evaluation_history
    if previous.availability == EvaluationHistoryAvailability.COMPLETE:
        return not previous.covered_submissions
    origins = tuple(
        receipt for receipt in run.receipts if receipt.decision_id == owner.admission_id
    )
    if len(origins) != 1:
        return False
    receipt = origins[0]
    return (
        isinstance(receipt.feedback, Accepted)
        and isinstance(receipt.decision, StartAttempt)
        and receipt.feedback.decision_id == receipt.decision_id == receipt.decision.decision_id
        and receipt.decision_id == owner.admission_id
        and receipt.decision.attempt_id == owner.attempt_id
        and receipt.decision.scope.owner == run.run_id
        and receipt.decision.scope.generation == run.generation
        and receipt.decision.scope.generation == owner.generation
        and receipt.decision.item_id == owner.item_id
        and receipt.decision.workspace == owner.workspace
        and receipt.decision.budget == owner.budget
        and not previous.covered_submissions
        and not previous.records
    )
