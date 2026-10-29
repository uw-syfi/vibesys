"""Admission of one measurement submission: who it is charged to, its identity and its budget.

The primitives the measurement area's admission paths share: whether a scope may still start work
(``current``), a plan's identity (``identity_of``), one allocation of a submission and its
ordinal (``allocate``) and whether an earlier submission's job and descendants were released.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    Verdict,
    continues_admitted_work,
    current_admission,
    released_owner,
)
from ._run_park import scope_in_flight
from ._values import digest
from .types.attempts import (
    AttemptPhase,
)
from .types.common import (
    MAX_SUBMISSIONS_PER_MEASUREMENT,
    ObservationStatus,
    RequestId,
    RevisionRef,
    RunStatus,
    Scope,
)
from .types.evaluation import (
    MeasurementIdentity,
    MeasurementResult,
    OwnedJob,
    PreparedSubmissionReceipt,
    RegisteredOwnedJob,
    SubmissionBudget,
    SubmitMeasurement,
)
from .types.kernel import AreaChange
from .types.records import Attribution, Outbound, inherit

if TYPE_CHECKING:
    from .types.common import DecisionId, InvocationRef, Observation
    from .types.evaluation import AgentMeasurementRequested, EvaluationState, MeasurementPlan
    from .types.job_observations import MeasurementFailure
    from .types.kernel import EvaluationContext
    from .types.measurement import BaseRef
    from .types.until import Within

__all__ = [
    "Origin",
    "allocate",
    "capped",
    "current",
    "descendants_released",
    "identity_of",
    "rejected",
    "submission_released",
]


def current(context: EvaluationContext, scope: Scope) -> Verdict[DecisionId | None]:
    if not continues_admitted_work(context.run) and not (
        context.run.status == RunStatus.DRAINING
        and scope_in_flight(context.sessions, context.records, scope)
    ):
        return Missing(ProofReason.UNRESOLVED)
    if scope.owner == context.run.run_id:
        return (
            Proven(None)
            if scope.generation == context.run.generation
            else Mismatch(ProofField.GENERATION)
        )
    owner = next((row for row in context.attempts.attempts if row.attempt_id == scope.owner), None)
    proof = current_admission(owner, scope, owner.admission_id if owner is not None else None)
    if not isinstance(proof, Proven):
        return proof
    if owner is None or owner.phase != AttemptPhase.ACTIVE or owner.closure is not None:
        return Missing(ProofReason.UNRESOLVED)
    if owner.terminal_reason is not None:
        return Missing(ProofReason.UNRESOLVED)
    return proof


def identity_of(plan: MeasurementPlan) -> Verdict[MeasurementIdentity]:
    if not isinstance(plan.candidate, RevisionRef):
        return Missing(ProofReason.ABSENT_CHECKPOINT)
    try:
        return Proven(MeasurementIdentity.from_plan(plan, plan.candidate))
    except ValueError:
        return Mismatch(ProofField.PAYLOAD)


def submission_released(
    state: EvaluationState, context: EvaluationContext, receipt: PreparedSubmissionReceipt
) -> Verdict[Observation]:
    rows = tuple(
        j
        for j in (*state.jobs, *state.registered_jobs)
        if (j.submission_id if isinstance(j, OwnedJob) else j.request_id) == receipt.request_id
    )
    if len(rows) != 1:
        return Missing(ProofReason.ABSENT_RESOURCE)
    job = rows[0]
    proof = released_owner(job, context.records.records)
    if not isinstance(proof, Proven):
        return proof
    return (
        proof
        if isinstance(descendants_released(context, job), Proven)
        else Missing(ProofReason.INCOMPLETE_MANIFEST)
    )


def descendants_released(
    context: EvaluationContext, job: OwnedJob | RegisteredOwnedJob
) -> Verdict[OwnedJob | RegisteredOwnedJob]:
    pending = list(
        dict.fromkeys(
            (*job.children, *(job.observation.descendants if job.observation is not None else ()))
        )
    )
    seen = set()
    while pending:
        resource = pending.pop()
        if resource in seen:
            continue
        seen.add(resource)
        rows = tuple(
            child
            for child in context.records.children
            if child.resource_id == resource and child.scope == job.scope
        )
        if len(rows) != 1:
            return Missing(ProofReason.INCOMPLETE_MANIFEST)
        child = rows[0]
        if not isinstance(released_owner(child, context.records.records), Proven):
            return Missing(ProofReason.UNRESOLVED)
        pending.extend(
            resource
            for mark in child.observation_watermarks
            for resource in mark.observation.descendants
        )
    return Proven(job)


@dataclass(frozen=True, slots=True)
class Origin:
    """Who a submission is charged to: the scope and the Measure decision, if any."""

    scope: Scope
    decision_id: DecisionId | None
    within: Within | None = None
    """The agent's limit; None for a Measure decision, whose limit is its wait's."""
    reuses: RequestId | None = None
    """The run that answers a first submission without a new one (`reused_run`)."""
    base: BaseRef | None = None
    """The revision an agent's candidate is compared with (`_base_of`)."""
    requested_by: InvocationRef | None = None
    """The turn that asked, when ``decision_id`` is absent."""


def allocate(
    state: EvaluationState,
    context: EvaluationContext,
    origin: Origin,
    plan: MeasurementPlan,
    budget: SubmissionBudget | None,
) -> AreaChange[EvaluationState]:
    scope, decision_id = origin.scope, origin.decision_id
    identity = identity_of(plan)
    authority = current(context, scope)
    if not isinstance(identity, Proven) or not isinstance(authority, Proven):
        return rejected(state, scope)
    if budget is None:
        budget = SubmissionBudget(
            scope=scope, identity=identity.value, limit=MAX_SUBMISSIONS_PER_MEASUREMENT
        )
    # New work is refused once the run's time is over, and a submission needs a positive limit.
    if not context.run.deadline_at > context.run.now_at or (
        origin.within is not None and origin.within.limit <= 0
    ):
        return rejected(state, scope)
    ordinal = len(budget.receipts) + 1
    key = digest(budget.identity)[:24]
    identity_id = RequestId(
        root=f"measurement:{scope.owner.root}:{scope.generation}:{key}:{ordinal}"
    )
    request = SubmitMeasurement(
        request_id=identity_id,
        scope=scope,
        admission_id=authority.value,
        decision_id=decision_id,
        plan=plan,
        # Only a first submission reuses: a retry after a failure measures afresh.
        reuses=origin.reuses if ordinal == 1 else None,
        base=origin.base,
        requested_by=None if decision_id is not None else origin.requested_by,
    )
    updated = budget.model_copy(
        update={
            "receipts": (
                *budget.receipts,
                PreparedSubmissionReceipt(request_id=identity_id, ordinal=ordinal),
            )
        }
    )
    budgets = (
        tuple(updated if b == budget else b for b in state.submission_budgets)
        if budget in state.submission_budgets
        else (*state.submission_budgets, updated)
    )
    # A measurement decision waits as long as its wait says; an agent's own submission, as
    # long as the limit parsed from its call says. Neither is a field of the plan.
    # The origin names the decision, if any: core's own base run and an agent's call have
    # none, whatever decision's step allocates them.
    outbound = (
        inherit(request)
        if decision_id is not None or origin.within is None
        else Outbound(request=request, until=origin.within)
    ).model_copy(update={"attribution": Attribution.OWN})
    return AreaChange(
        state=state.model_copy(update={"submission_budgets": budgets}), requests=(outbound,)
    )


def rejected(
    state: EvaluationState, scope: Scope, failure: MeasurementFailure | None = None
) -> AreaChange[EvaluationState]:
    return AreaChange(
        state=state,
        events=(
            MeasurementResult(
                scope=scope, evidence=(), status=ObservationStatus.REJECTED, failure=failure
            ),
        ),
    )


def capped(
    event: AgentMeasurementRequested, *, deadline_at: float | None = None, now_at: float = 0.0
) -> Within:
    """The agent's limit, no longer than the plan or the turn permit's deadline."""
    within = event.within
    limit = min(within.limit, event.plan.time_bound)
    if deadline_at is not None:
        limit = min(limit, max(deadline_at - now_at, 0.0))
    return within.model_copy(update={"limit": limit})
