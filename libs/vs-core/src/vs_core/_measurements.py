"""Pure measurement submission, canonical evidence ingress and job cleanup.

Only committed source facts grant observation authority. Submission ordinals and
requests are allocated together; scientific references retain their first source
receipt. Continuation wakes carry the exact facts before the atomic job update.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._evaluation_history import produce_history
from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    Verdict,
    accepted_receipt_for,
    current_admission,
    current_closure,
    fresh_observation,
    observation_for,
    operation_for,
    released_owner,
    request_matches,
    submission_budget_for,
)
from ._registry import ContractError
from ._values import digest
from .types.attempts import (
    AttemptEvaluationHistoryUpdated,
    AttemptPhase,
    CloseAttemptScope,
    SnapshotAndRetain,
)
from .types.common import (
    AttemptId,
    AttemptRef,
    EvidenceKind,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    RequestId,
    RevisionRef,
    RunStatus,
)
from .types.evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    ContinuationJobsChanged,
    EvidenceAcceptanceReceipt,
    InspectOwnedJob,
    JobObserved,
    JobsDrainRequested,
    JobTerminationRequested,
    MeasurementIdentity,
    MeasurementRequested,
    MeasurementResult,
    MeasurementSubmissionObserved,
    ObservedJobFacts,
    ObserveOwnedJob,
    OwnedJob,
    PreparedSubmissionReceipt,
    RegisteredJobObserved,
    RegisteredJobRequested,
    RegisteredOwnedJob,
    SnapshotResultRef,
    SubmissionBudget,
    SubmitMeasurement,
    UnobservedJobFacts,
)
from .types.evaluation_history import EvaluationStageOutcome
from .types.job_observations import MeasurementFailure
from .types.kernel import AreaChange
from .types.strategy import Measure

if TYPE_CHECKING:
    from .types.common import DecisionId, Observation, ResourceId, Scope
    from .types.evaluation import EvaluationEvent, EvaluationState, EvidenceRef, MeasurementPlan
    from .types.intents import Intent, Request
    from .types.kernel import EvaluationContext

__all__ = ["advance"]


def _current(context: EvaluationContext, scope: Scope) -> Verdict[DecisionId | None]:
    if context.run.status != RunStatus.RUNNING:
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


def _origin(context: EvaluationContext, event: MeasurementRequested) -> Verdict[Measure]:
    rows = tuple(
        receipt
        for receipt in context.run.receipts
        if isinstance(receipt.decision, Measure)
        and receipt.decision.scope == event.scope
        and receipt.decision.plan == event.plan
    )
    if not rows:
        return Missing(ProofReason.ABSENT_RECEIPT)
    # Identical measurements can be proposed again after conclusive failure.
    # The latest accepted command supplies correlation, never a new budget key.
    receipt = rows[-1]
    proof = accepted_receipt_for(context.run.receipts, receipt.decision_id, receipt.decision)
    if not isinstance(proof, Proven):
        return proof
    return (
        Proven(receipt.decision)
        if isinstance(receipt.decision, Measure)
        else Mismatch(ProofField.PAYLOAD)
    )


def _resolved_plan(
    context: EvaluationContext, scope: Scope, plan: MeasurementPlan
) -> Verdict[MeasurementPlan]:
    if not isinstance(plan.candidate, SnapshotResultRef):
        return Proven(plan)
    revision = _snapshot_revision(context, scope, plan.candidate)
    return (
        Proven(plan.model_copy(update={"candidate": revision.value}))
        if isinstance(revision, Proven)
        else revision
    )


def _snapshot_revision(
    context: EvaluationContext, scope: Scope, candidate: SnapshotResultRef
) -> Verdict[RevisionRef]:
    rows = tuple(row for row in context.intents.intents if row.request_id == candidate.request_id)
    if len(rows) != 1:
        return Missing(ProofReason.ABSENT_REQUEST)
    row = rows[0]
    if not isinstance(row.request, SnapshotAndRetain) or row.request.scope != scope:
        return Mismatch(ProofField.SCOPE)
    if not isinstance(request_matches(row, row.request), Proven):
        return Mismatch(ProofField.PAYLOAD)
    proof = observation_for(row, row.observation)
    if not isinstance(proof, Proven):
        return proof
    observation = proof.value
    if (
        not observation.accepted
        or not observation.terminal
        or observation.status != ObservationStatus.SUCCEEDED
    ):
        return Missing(ProofReason.ABSENT_CHECKPOINT)
    checkpoints = tuple(
        checkpoint
        for owner in context.attempts.attempts
        if owner.attempt_id == scope.owner and owner.generation == scope.generation
        for checkpoint in owner.checkpoints
        if checkpoint.request_id == row.request_id
    )
    return (
        Proven(checkpoints[0].revision)
        if len(checkpoints) == 1
        else Missing(ProofReason.ABSENT_CHECKPOINT)
    )


def _identity(plan: MeasurementPlan) -> Verdict[MeasurementIdentity]:
    if not isinstance(plan.candidate, RevisionRef):
        return Missing(ProofReason.ABSENT_CHECKPOINT)
    try:
        return Proven(MeasurementIdentity.from_plan(plan, plan.candidate))
    except ValueError:
        return Mismatch(ProofField.PAYLOAD)


def _budget_ready(
    state: EvaluationState, context: EvaluationContext, budget: SubmissionBudget | None
) -> Verdict[SubmissionBudget | None]:
    if budget is None or not budget.receipts:
        return Proven(budget)
    latest = budget.receipts[-1]
    if not isinstance(latest, PreparedSubmissionReceipt) or latest.observation is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    observation = latest.observation
    if (
        observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        or not observation.terminal
    ):
        return Missing(ProofReason.UNRESOLVED)
    if latest.failure != MeasurementFailure.INFRASTRUCTURE or (
        observation.accepted
        and not isinstance(_submission_released(state, context, latest), Proven)
    ):
        return Missing(ProofReason.UNRESOLVED)
    if len(budget.receipts) >= budget.limit:
        return Missing(ProofReason.ABSENT_CHARGE)
    return Proven(budget)


def _submission_released(
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
    proof = released_owner(job, context.intents.intents)
    if not isinstance(proof, Proven):
        return proof
    return (
        proof
        if isinstance(_descendants_released(context, job), Proven)
        else Missing(ProofReason.INCOMPLETE_MANIFEST)
    )


def _descendants_released(
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
            for child in context.intents.children
            if child.resource_id == resource and child.scope == job.scope
        )
        if len(rows) != 1:
            return Missing(ProofReason.INCOMPLETE_MANIFEST)
        child = rows[0]
        if not isinstance(released_owner(child, context.intents.intents), Proven):
            return Missing(ProofReason.UNRESOLVED)
        pending.extend(
            resource
            for mark in child.observation_watermarks
            for resource in mark.observation.descendants
        )
    return Proven(job)


def _reused(
    state: EvaluationState, context: EvaluationContext, scope: Scope, plan: MeasurementPlan
) -> tuple[EvidenceRef, ...]:
    if plan.purpose != "profile":
        return ()
    return tuple(
        proof.value
        for evidence in state.evidence
        if isinstance(proof := _reusable_capture(state, context, scope, plan, evidence), Proven)
    )


def _reusable_capture(
    state: EvaluationState,
    context: EvaluationContext,
    scope: Scope,
    plan: MeasurementPlan,
    evidence: EvidenceRef,
) -> Verdict[EvidenceRef]:
    identity = _identity(plan)
    if not isinstance(identity, Proven):
        return identity
    jobs = tuple(
        j
        for j in (*state.jobs, *state.registered_jobs)
        if (j.submission_id if isinstance(j, OwnedJob) else j.request_id) == evidence.source_request
        and evidence in j.evidence
    )
    if len(jobs) != 1:
        return Missing(ProofReason.ABSENT_RESOURCE)
    job = jobs[0]
    source = next(
        (r for r in context.intents.intents if r.request_id == evidence.source_request), None
    )
    if source is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    binding = _job_source(state, context, job, source)
    expected = _job_identity(job)
    if (
        not isinstance(binding, Proven)
        or not isinstance(expected, Proven)
        or expected.value != identity.value
        or evidence.scope != scope
    ):
        return Mismatch(ProofField.NORMALIZATION)
    receipt = evidence.acceptance_receipt
    if (
        evidence.kind != EvidenceKind.PROFILING
        or evidence.provenance != "trusted"
        or evidence.status != ObservationStatus.SUCCEEDED
        or receipt is None
        or not receipt.observation.terminal
    ):
        return Missing(ProofReason.UNRESOLVED)
    # A retained reference must still match its independently normalized source.
    fields = (
        evidence.purpose == identity.value.purpose,
        evidence.candidate == identity.value.candidate,
        evidence.evaluator_digest == identity.value.evaluator_digest,
        evidence.workload_digest == identity.value.workload_digest,
        evidence.environment_digest == identity.value.environment_digest,
    )
    return Proven(evidence) if all(fields) else Mismatch(ProofField.DIGEST)


def _requested(
    state: EvaluationState, context: EvaluationContext, event: MeasurementRequested
) -> AreaChange[EvaluationState]:
    authority = _current(context, event.scope)
    origin = _origin(context, event)
    resolved = _resolved_plan(context, event.scope, event.plan)
    if (
        not isinstance(authority, Proven)
        or not isinstance(origin, Proven)
        or not isinstance(resolved, Proven)
    ):
        return _rejected(state, event.scope)
    plan = resolved.value
    identity = _identity(plan)
    if not isinstance(identity, Proven):
        return _rejected(state, event.scope)
    reuse = _reused(state, context, event.scope, plan)
    if reuse:
        return AreaChange(
            state=state,
            events=(
                MeasurementResult(
                    scope=event.scope, evidence=reuse, status=ObservationStatus.SUCCEEDED
                ),
            ),
        )
    matches = tuple(
        b
        for b in state.submission_budgets
        if b.scope == event.scope and b.identity == identity.value
    )
    budget = matches[0] if matches else None
    # Replayed commands never allocate another ordinal, including changed timing.
    if any(
        isinstance(row.request, SubmitMeasurement)
        and row.request.decision_id == origin.value.decision_id
        for row in context.intents.intents
    ):
        return AreaChange(state=state)
    if len(matches) > 1 or not isinstance(_budget_ready(state, context, budget), Proven):
        failure = (
            next(
                (
                    r.failure
                    for r in reversed(budget.receipts)
                    if isinstance(r, PreparedSubmissionReceipt)
                    and r.failure == MeasurementFailure.WORKLOAD
                ),
                None,
            )
            if budget is not None
            else None
        )
        return _rejected(state, event.scope, failure)
    return _allocate(state, context, origin.value, plan, budget)


def _allocate(
    state: EvaluationState,
    context: EvaluationContext,
    origin: Measure,
    plan: MeasurementPlan,
    budget: SubmissionBudget | None,
) -> AreaChange[EvaluationState]:
    scope = origin.scope
    identity = _identity(plan)
    authority = _current(context, scope)
    if not isinstance(identity, Proven) or not isinstance(authority, Proven):
        return _rejected(state, scope)
    if budget is not None and budget.limit > context.run.limits.max_measurement_submissions:
        return _rejected(state, scope)
    if budget is None:
        if plan.submission_limit > context.run.limits.max_measurement_submissions:
            return _rejected(state, scope)
        budget = SubmissionBudget(scope=scope, identity=identity.value, limit=plan.submission_limit)
    deadline = min(plan.deadline_at, context.run.deadline_at)
    if deadline <= context.run.now_at:
        return _rejected(state, scope)
    ordinal = len(budget.receipts) + 1
    key = digest(budget.identity)[:24]
    identity_id = RequestId(
        root=f"measurement:{scope.owner.root}:{scope.generation}:{key}:{ordinal}"
    )
    request = SubmitMeasurement(
        request_id=identity_id,
        scope=scope,
        admission_id=authority.value,
        decision_id=origin.decision_id,
        deadline_at=deadline,
        plan=plan,
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
    return AreaChange(
        state=state.model_copy(update={"submission_budgets": budgets}), requests=(request,)
    )


def _rejected(
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


def _held_mismatch(
    held: Observation | None, observation: Observation, *, later: bool
) -> Mismatch | Missing | None:
    if not later:
        return None if held == observation else Mismatch(ProofField.PAYLOAD)
    if held is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    if observation.sequence < held.sequence:
        return Mismatch(ProofField.SEQUENCE)
    if observation.sequence == held.sequence and held != observation:
        return Mismatch(ProofField.PAYLOAD)
    return None


def _source(
    context: EvaluationContext, observation: Observation, *, later: bool = False
) -> Verdict[Intent]:
    """The committed request an observation belongs to.

    A job's observations carry its submission's request id, but only the submission's
    own observation goes through the intent ledger. With ``later``, the ledger must
    hold the submission's observation, and the incoming one may be newer than it: an
    earlier or conflicting one is refused, and freshness against the job follows.
    """
    rows = tuple(row for row in context.intents.intents if row.request_id == observation.request_id)
    if len(rows) != 1:
        return Missing(ProofReason.ABSENT_REQUEST) if not rows else Mismatch(ProofField.REQUEST_ID)
    row = rows[0]
    canonical = request_matches(row, row.request)
    if not isinstance(canonical, Proven):
        return canonical
    proof = observation_for(row, observation)
    if not isinstance(proof, Proven):
        return proof
    mismatch = _held_mismatch(row.observation, observation, later=later)
    if mismatch is not None:
        return mismatch
    if row.lifecycle != LifecycleClass.OWNED_JOB:
        return Mismatch(ProofField.LIFECYCLE)
    return Proven(row)


def _submission_observed(
    state: EvaluationState, context: EvaluationContext, event: MeasurementSubmissionObserved
) -> AreaChange[EvaluationState]:
    proof = _source(context, event.observation)
    if not isinstance(proof, Proven):
        return AreaChange(state=state)
    source = proof.value
    request = source.request
    if not isinstance(request, SubmitMeasurement):
        return AreaChange(state=state)
    if _resource_taken(state, event.observation, source.request_id):
        return AreaChange(state=state)
    budget = submission_budget_for(request, state.submission_budgets, context.run.receipts)
    if not isinstance(budget, Proven):
        return AreaChange(state=state)
    updated = _submission_receipt(budget.value, event, source)
    if not isinstance(updated, Proven):
        return AreaChange(state=state)
    state = state.model_copy(
        update={
            "submission_budgets": tuple(
                updated.value if b == budget.value else b for b in state.submission_budgets
            )
        }
    )
    return _submission_job(state, context, event, source, request)


def _owner_request(job: OwnedJob | RegisteredOwnedJob) -> RequestId:
    return job.submission_id if isinstance(job, OwnedJob) else job.request_id


def _resource_taken(state: EvaluationState, observation: Observation, owner: RequestId) -> bool:
    """Whether another submission already owns this external resource id.

    Ownership, cancellation and release are all addressed by resource id, so a second
    owner would orphan both jobs. The colliding observation is refused whole.
    """
    return observation.resource_id is not None and any(
        j.resource_id == observation.resource_id and _owner_request(j) != owner
        for j in (*state.jobs, *state.registered_jobs)
    )


def _canonical_failure(
    observation: Observation, source: Intent, claim: MeasurementFailure | None
) -> MeasurementFailure | None:
    """Restrict the caller's failure claim to what the committed facts allow.

    A classification can only narrow what the canonical observation proves. It never
    grants retry authority after execution succeeded, never contradicts committed
    scientific facts, and never exists before a conclusive terminal observation.
    """
    if claim is None or not _conclusive(observation):
        return None
    if observation.accepted:
        if observation.status == ObservationStatus.SUCCEEDED:
            return None
        if claim == MeasurementFailure.INFRASTRUCTURE and source.evaluation_result is not None:
            return MeasurementFailure.UNKNOWN
    return claim


def _conclusive(observation: Observation | None) -> bool:
    return (
        observation is not None
        and observation.terminal
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def _submission_receipt(
    budget: SubmissionBudget, event: MeasurementSubmissionObserved, source: Intent
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
            "failure": _canonical_failure(event.observation, source, event.failure),
        }
    )
    return Proven(
        budget.model_copy(
            update={"receipts": tuple(updated if r == receipt else r for r in budget.receipts)}
        )
    )


def _submission_job(
    state: EvaluationState,
    context: EvaluationContext,
    event: MeasurementSubmissionObserved,
    source: Intent,
    request: SubmitMeasurement,
) -> AreaChange[EvaluationState]:
    observation = event.observation
    jobs = tuple(j for j in state.jobs if j.submission_id == source.request_id)
    if not jobs and observation.accepted and observation.resource_id is not None:
        job = OwnedJob(
            resource_id=observation.resource_id,
            submission_id=source.request_id,
            scope=request.scope,
            plan=request.plan,
            status=ObservationStatus.PENDING,
        )
        state = state.model_copy(update={"jobs": (*state.jobs, job)})
        # The executor reports a submission once; every later fact about the job comes
        # from observing it, so core starts that cycle here. Each observed non-terminal
        # job asks for the next one (see _job_observed), so the cycle ends with the job.
        requests = (
            ()
            if context.run.status == RunStatus.TERMINAL
            else (_job_request(ObserveOwnedJob, job, context, "observe"),)
        )
        return AreaChange(state=state, requests=requests)
    if observation.terminal and not observation.accepted:
        return AreaChange(
            state=state,
            events=(
                MeasurementResult(
                    scope=request.scope,
                    source_request=observation.request_id,
                    evidence=(),
                    status=observation.status,
                    failure=event.failure,
                ),
            ),
        )
    return AreaChange(state=state)


def _registered_requested(
    state: EvaluationState, context: EvaluationContext, event: RegisteredJobRequested
) -> AreaChange[EvaluationState]:
    request = event.request
    proof = operation_for(context.run.receipts, request)
    current = _current(context, request.scope)
    if not isinstance(proof, Proven) or not isinstance(current, Proven):
        return _rejected(state, request.scope)
    if (
        request.operation.schema_ref.lifecycle != LifecycleClass.OWNED_JOB
        or request.request_id is None
    ):
        return _rejected(state, request.scope)
    identity = proof.value.registered_measurement
    if event.expected_measurement != identity:
        return _rejected(state, request.scope)
    existing = tuple(
        j
        for j in state.registered_jobs
        if j.request_id == request.request_id or j.operation_id == request.operation_id
    )
    if existing:
        return (
            AreaChange(state=state)
            if len(existing) == 1
            and existing[0].scope == request.scope
            and existing[0].expected_measurement == identity
            else _rejected(state, request.scope)
        )
    budgets = state.submission_budgets
    if identity is not None:
        matches = tuple(b for b in budgets if b.scope == request.scope and b.identity == identity)
        if matches or context.run.limits.max_measurement_submissions < 1:
            return _rejected(state, request.scope)
        budgets = (
            *budgets,
            SubmissionBudget(
                scope=request.scope,
                identity=identity,
                limit=1,
                receipts=(PreparedSubmissionReceipt(request_id=request.request_id, ordinal=1),),
            ),
        )
    job = RegisteredOwnedJob(
        operation_id=request.operation_id,
        expected_measurement=identity,
        request_id=request.request_id,
        scope=request.scope,
        resource_pool=event.resource_pool,
    )
    return AreaChange(
        state=state.model_copy(
            update={"registered_jobs": (*state.registered_jobs, job), "submission_budgets": budgets}
        ),
        requests=(request,),
    )


def _job_identity(job: OwnedJob | RegisteredOwnedJob) -> Verdict[MeasurementIdentity]:
    if isinstance(job, OwnedJob):
        return _identity(job.plan)
    return (
        Proven(job.expected_measurement)
        if job.expected_measurement is not None
        else Missing(ProofReason.ABSENT_DECLARATION)
    )


_ACCURACY_GATED = (EvidenceKind.BENCHMARK, EvidenceKind.CORRECTNESS)


def _required_stages(identity: MeasurementIdentity, kind: EvidenceKind) -> frozenset[str]:
    """Stages that must have passed before this kind of evidence can be trusted.

    Correctness is gated by the plan's declared accuracy stage; a plan that declares
    none requires every stage, and a benchmark rate always needs every stage.
    """
    if kind == EvidenceKind.CORRECTNESS and identity.accuracy_stage is not None:
        return frozenset((identity.accuracy_stage,))
    return frozenset(s.stage_id for s in identity.stages)


def _scientific_evidence(
    event: JobObserved | RegisteredJobObserved,
    evidence: EvidenceRef,
    identity: MeasurementIdentity,
) -> Verdict[EvidenceRef]:
    facts = event.evaluation_result
    if evidence.status != ObservationStatus.SUCCEEDED:
        return Proven(evidence)
    if facts is None:
        return Missing(ProofReason.INCOMPLETE_HISTORY)
    outcomes = {stage.stage_id: stage.outcome for stage in facts.stages}
    gated = evidence.kind in _ACCURACY_GATED
    unsound = (
        # Scientific success cannot ride an execution that did not succeed.
        event.observation.status != ObservationStatus.SUCCEEDED
        or (gated and not facts.accuracy_passed)
        or any(
            outcomes.get(stage_id) != EvaluationStageOutcome.PASSED
            for stage_id in _required_stages(identity, evidence.kind)
        )
        or (evidence.kind == EvidenceKind.BENCHMARK and facts.failed_benchmark is not None)
    )
    return Mismatch(ProofField.STATUS) if unsound else Proven(evidence)


def _evidence(
    job: OwnedJob | RegisteredOwnedJob,
    event: JobObserved | RegisteredJobObserved,
    evidence: EvidenceRef,
) -> Verdict[EvidenceRef]:
    observation = event.observation
    identity = _job_identity(job)
    if not isinstance(identity, Proven):
        return identity
    science = _scientific_evidence(event, evidence, identity.value)
    expected = identity.value
    request_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    checks = (
        (ProofField.SCOPE, evidence.scope, job.scope),
        (ProofField.REQUEST_ID, evidence.source_request, request_id),
        (ProofField.REVISION, evidence.candidate, expected.candidate),
        (ProofField.SEQUENCE, evidence.observation_sequence, observation.sequence),
        (ProofField.PAYLOAD, evidence.purpose, expected.purpose),
        (ProofField.DIGEST, evidence.evaluator_digest, expected.evaluator_digest),
        (ProofField.DIGEST, evidence.workload_digest, expected.workload_digest),
        (ProofField.DIGEST, evidence.environment_digest, expected.environment_digest),
    )
    for field, actual, canonical in checks:
        if actual != canonical:
            return Mismatch(field)
    if (
        not isinstance(science, Proven)
        or not observation.accepted
        or not observation.terminal
        or observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    ):
        return Missing(ProofReason.UNRESOLVED)
    if evidence.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING):
        return Missing(ProofReason.UNRESOLVED)
    # Receipt status is scientific, and can differ from successful execution.
    receipt_observation = observation.model_copy(update={"status": evidence.status})
    if (
        evidence.acceptance_receipt is not None
        and evidence.acceptance_receipt.observation != receipt_observation
    ):
        return Mismatch(ProofField.PAYLOAD)
    return Proven(
        evidence.model_copy(
            update={
                "acceptance_receipt": EvidenceAcceptanceReceipt(observation=receipt_observation)
            }
        )
    )


def _observed_owner(
    state: EvaluationState, context: EvaluationContext, event: JobObserved | RegisteredJobObserved
) -> Verdict[OwnedJob | RegisteredOwnedJob]:
    proof = _source(context, event.observation, later=isinstance(event, JobObserved))
    if not isinstance(proof, Proven):
        return proof
    source = proof.value
    rows = (
        tuple(j for j in state.jobs if j.resource_id == event.resource_id)
        if isinstance(event, JobObserved)
        else tuple(j for j in state.registered_jobs if j.operation_id == event.operation_id)
    )
    if len(rows) != 1:
        return Missing(ProofReason.ABSENT_RESOURCE)
    job = rows[0]
    binding = _job_source(state, context, job, source)
    if not isinstance(binding, Proven):
        return binding
    if _resource_taken(state, event.observation, source.request_id):
        return Mismatch(ProofField.RESOURCE_ID)
    return _incoming_job(job, source, event)


def _job_source(
    state: EvaluationState,
    context: EvaluationContext,
    job: OwnedJob | RegisteredOwnedJob,
    source: Intent,
) -> Verdict[OwnedJob | RegisteredOwnedJob]:
    request_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    if request_id != source.request_id or job.scope != source.request.scope:
        return Mismatch(ProofField.REQUEST_ID)
    if isinstance(job, OwnedJob):
        return _builtin_source(state, context, job, source)
    if not isinstance(source.request, ExecuteRegisteredOperation):
        return Mismatch(ProofField.LIFECYCLE)
    origin = operation_for(context.run.receipts, source.request)
    if not isinstance(origin, Proven):
        return origin
    return (
        Proven(job)
        if origin.value.registered_measurement == job.expected_measurement
        else Mismatch(ProofField.NORMALIZATION)
    )


def _builtin_source(
    state: EvaluationState, context: EvaluationContext, job: OwnedJob, source: Intent
) -> Verdict[OwnedJob]:
    request = source.request
    if not isinstance(request, SubmitMeasurement) or request.plan != job.plan:
        return Mismatch(ProofField.PAYLOAD)
    receipt = accepted_receipt_for(context.run.receipts, request.decision_id, None)
    if not isinstance(receipt, Proven):
        return receipt
    decision = receipt.value.decision
    if (
        not isinstance(decision, Measure)
        or decision.scope != request.scope
        or source.request_id not in receipt.value.request_ids
    ):
        return Mismatch(ProofField.REQUEST_ID)
    resolved = _resolved_plan(context, request.scope, decision.plan)
    if not isinstance(resolved, Proven):
        return resolved
    if resolved.value != request.plan:
        return Mismatch(ProofField.PAYLOAD)
    budget = submission_budget_for(request, state.submission_budgets, context.run.receipts)
    return Proven(job) if isinstance(budget, Proven) else budget


def _incoming_job(
    job: OwnedJob | RegisteredOwnedJob, source: Intent, event: JobObserved | RegisteredJobObserved
) -> Verdict[OwnedJob | RegisteredOwnedJob]:
    observation = event.observation
    if observation.resource_id is None or (
        job.resource_id is not None and observation.resource_id != job.resource_id
    ):
        return Mismatch(ProofField.RESOURCE_ID)
    history = (job.observation,) if job.observation is not None else ()
    proof = fresh_observation(history, observation, complete=True)
    if not isinstance(proof, Proven):
        return proof
    if source.observation == observation and source.evaluation_result != event.evaluation_result:
        return Mismatch(ProofField.PAYLOAD)
    side = _side_facts(job, event)
    if not isinstance(side, Proven):
        return side
    if job.observation is not None and (
        observation.observed_at < job.observation.observed_at
        or (
            job.terminal
            and (
                not observation.terminal
                or observation.status in (ObservationStatus.PENDING, ObservationStatus.UNKNOWN)
            )
        )
        or (job.released and not observation.released)
    ):
        return Mismatch(ProofField.SEQUENCE)
    return Proven(job)


def _side_facts(
    job: OwnedJob | RegisteredOwnedJob, event: JobObserved | RegisteredJobObserved
) -> Verdict[OwnedJob | RegisteredOwnedJob]:
    identity = _job_identity(job)
    stages = {s.stage_id for s in identity.value.stages} if isinstance(identity, Proven) else set()
    if (
        event.progress is not None
        and event.progress.stage_id is not None
        and isinstance(identity, Proven)
        and event.progress.stage_id not in stages
    ):
        return Mismatch(ProofField.PAYLOAD)
    if event.evaluation_result is not None and (
        not isinstance(identity, Proven)
        or any(s.stage_id not in stages for s in event.evaluation_result.stages)
    ):
        return Mismatch(ProofField.PAYLOAD)
    return Proven(job)


def _store_evidence(
    state: EvaluationState,
    job: OwnedJob | RegisteredOwnedJob,
    event: JobObserved | RegisteredJobObserved,
) -> tuple[tuple[EvidenceRef, ...], tuple[EvidenceRef, ...], bool]:
    """Accept evidence keyed by (source request, evidence id).

    The same EvidenceId from two jobs is two records. A repeat of one key with
    different content is refused, and the caller reports that refusal instead of
    dropping it silently.
    """
    accepted = list(job.evidence)
    ledger = list(state.evidence)
    refused = False
    for evidence in event.evidence:
        verdict = _evidence(job, event, evidence)
        if not isinstance(verdict, Proven):
            continue
        incoming = verdict.value
        previous = next((e for e in ledger if e.key == incoming.key), None)
        if previous is None:
            ledger.append(incoming)
            accepted.append(incoming)
        elif previous != incoming:
            refused = True
        elif incoming not in accepted:
            accepted.append(incoming)
    return tuple(accepted), tuple(ledger), refused


def _job_observed(
    state: EvaluationState, context: EvaluationContext, event: JobObserved | RegisteredJobObserved
) -> AreaChange[EvaluationState]:
    owner = _observed_owner(state, context, event)
    if not isinstance(owner, Proven):
        # Unknown, foreign, early or stale observations carry unverified scope and
        # identity, so they change nothing and tell the strategy nothing.
        return AreaChange(state=state)
    if owner.value.observation == event.observation:
        return AreaChange(state=state)
    job = owner.value
    observation = event.observation
    if observation.resource_id is None:
        return AreaChange(state=state)
    accepted, ledger, refused = _store_evidence(state, job, event)
    previous = (
        ObservedJobFacts(
            resource_id=observation.resource_id,
            observation=job.observation,
            progress=job.progress,
            evidence=job.evidence,
        )
        if job.observation is not None
        else UnobservedJobFacts(resource_id=observation.resource_id)
    )
    updated = job.model_copy(
        update={
            "resource_id": observation.resource_id,
            "observation": observation,
            "progress": event.progress if event.progress is not None else job.progress,
            "status": observation.status,
            "terminal": observation.terminal,
            "released": observation.released,
            "children": tuple(dict.fromkeys((*job.children, *observation.children))),
            "evidence": tuple(accepted),
        }
    )
    state = state.model_copy(
        update={
            "jobs": tuple(updated if j == job else j for j in state.jobs)
            if isinstance(job, OwnedJob)
            else state.jobs,
            "registered_jobs": tuple(updated if j == job else j for j in state.registered_jobs)
            if isinstance(job, RegisteredOwnedJob)
            else state.registered_jobs,
            "evidence": tuple(ledger),
        }
    )
    state = _job_budget(state, context, updated)
    wake = ContinuationJobsChanged(
        resource_id=observation.resource_id, observation=observation, previous=previous
    )
    requests: tuple[Request, ...] = ()
    events: tuple[MeasurementResult, ...] = ()
    source_id = updated.submission_id if isinstance(updated, OwnedJob) else updated.request_id
    if _conclusive(observation):
        newly = tuple(e for e in accepted if e not in job.evidence)
        # A refused evidence id is reported as an unclassified failure, never hidden.
        failure = MeasurementFailure.UNKNOWN if refused else None
        if not _conclusive(job.observation):
            events = (
                MeasurementResult(
                    scope=job.scope,
                    source_request=source_id,
                    evidence=tuple(accepted),
                    status=observation.status,
                    failure=failure,
                ),
            )
        elif newly or refused:
            # Late evidence is published once, as the delta, never replayed or dropped.
            events = (
                MeasurementResult(
                    scope=job.scope,
                    source_request=source_id,
                    evidence=newly,
                    status=observation.status,
                    failure=failure,
                ),
            )
        if observation.accepted and not updated.evidence:
            requests = (_job_request(CollectEvidence, updated, context, "evidence"),)
    elif observation.accepted:
        requests = (_job_request(ObserveOwnedJob, updated, context, "observe"),)
    return AreaChange(
        state=state,
        signals=(*_history_signals(state, context, job.scope), wake),
        requests=requests if context.run.status != RunStatus.TERMINAL else (),
        events=events,
    )


def _job_budget(
    state: EvaluationState, context: EvaluationContext, job: OwnedJob | RegisteredOwnedJob
) -> EvaluationState:
    if job.observation is None:
        return state
    source_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    source = next((r for r in context.intents.intents if r.request_id == source_id), None)
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
    updated = _submission_receipt(
        proof.value,
        MeasurementSubmissionObserved(
            observation=job.observation, failure=receipt.failure if receipt is not None else None
        ),
        source,
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


def _history_signals(
    state: EvaluationState, context: EvaluationContext, scope: Scope
) -> tuple[AttemptEvaluationHistoryUpdated, ...]:
    if not isinstance(scope.owner, AttemptId):
        return ()
    owner = next(
        (
            o
            for o in context.attempts.attempts
            if o.attempt_id == scope.owner and o.generation == scope.generation
        ),
        None,
    )
    if owner is None:
        return ()
    history = produce_history(scope, state, context.intents, owner, context.run)
    if history == owner.evaluation_history or any(
        record not in history.records for record in owner.evaluation_history.records
    ):
        return ()
    return (
        AttemptEvaluationHistoryUpdated(
            attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
            history=history,
        ),
    )


def _job_request(
    model: type[ObserveOwnedJob]
    | type[InspectOwnedJob]
    | type[CancelOwnedJob]
    | type[CollectEvidence],
    job: OwnedJob | RegisteredOwnedJob,
    context: EvaluationContext,
    action: str,
) -> Request:
    if job.resource_id is None:
        raise ContractError(("job", "resource_id"), "cannot request an unidentified job")
    source = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    observation = job.observation
    sequence = observation.sequence if observation is not None else 0
    canonical = next((r for r in context.intents.intents if r.request_id == source), None)
    return model(
        request_id=RequestId(root=f"measurement:{source.root}:{action}:{sequence}"),
        scope=job.scope,
        admission_id=canonical.request.admission_id if canonical is not None else None,
        resource_id=job.resource_id,
        deadline_at=context.run.now_at
        + (
            context.run.limits.cancellation_bound
            if model is CancelOwnedJob
            else context.run.limits.reconciliation_bound
        ),
    )


def _terminate(
    state: EvaluationState, context: EvaluationContext, resource: ResourceId
) -> AreaChange[EvaluationState]:
    rows = tuple(j for j in (*state.jobs, *state.registered_jobs) if j.resource_id == resource)
    if len(rows) != 1:
        return AreaChange(state=state)
    job = rows[0]
    if isinstance(released_owner(job, context.intents.intents), Proven):
        return AreaChange(state=state)
    source_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    source = next((r for r in context.intents.intents if r.request_id == source_id), None)
    if (
        source is None
        or not isinstance(request_matches(source, source.request), Proven)
        or not isinstance(_job_source(state, context, job, source), Proven)
    ):
        return AreaChange(state=state)
    if (
        job.observation is None
        or job.observation.status == ObservationStatus.UNKNOWN
        or not job.observation.accepted
    ):
        return AreaChange(
            state=state, requests=(_job_request(InspectOwnedJob, job, context, "inspect"),)
        )
    if job.terminal:
        return AreaChange(
            state=state, requests=(_job_request(InspectOwnedJob, job, context, "release"),)
        )
    return AreaChange(state=state, requests=(_job_request(CancelOwnedJob, job, context, "cancel"),))


def _drain(
    state: EvaluationState, context: EvaluationContext, event: JobsDrainRequested
) -> AreaChange[EvaluationState]:
    owners = tuple(
        o
        for o in context.attempts.attempts
        if o.attempt_id == event.scope.owner and o.generation == event.scope.generation
    )
    if len(owners) != 1:
        return AreaChange(state=state)
    owner = owners[0]
    proof = current_closure(owner, owner.closure)
    authority = next((r for r in context.intents.intents if r.request_id == event.authority), None)
    if (
        not isinstance(proof, Proven)
        or proof.value.authority != event.authority
        or proof.value.disposition != event.disposition
    ):
        return AreaChange(state=state)
    if (
        authority is None
        or not isinstance(authority.request, CloseAttemptScope)
        or not isinstance(request_matches(authority, authority.request), Proven)
        or authority.request.scope != event.scope
    ):
        return AreaChange(state=state)
    requests = tuple(
        request
        for job in (*state.jobs, *state.registered_jobs)
        if job.scope == event.scope and job.resource_id is not None
        for request in _terminate(state, context, job.resource_id).requests
    )
    return AreaChange(state=state, requests=requests)


def advance(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Preserve sibling continuations while consuming every measurement event."""
    match event:
        case MeasurementRequested():
            return _requested(state, context, event)
        case MeasurementSubmissionObserved():
            return _submission_observed(state, context, event)
        case RegisteredJobRequested():
            return _registered_requested(state, context, event)
        case JobObserved() | RegisteredJobObserved():
            return _job_observed(state, context, event)
        case JobTerminationRequested():
            return _terminate(state, context, event.resource_id)
        case JobsDrainRequested():
            return _drain(state, context, event)
        case _:
            raise ContractError(("event", event.kind), "not a measurement event")
