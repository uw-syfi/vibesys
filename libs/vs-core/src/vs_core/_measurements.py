"""Pure measurement submission, canonical evidence ingress and job cleanup.

Only committed source facts grant observation authority. Submission ordinals and
requests are allocated together; scientific references retain their first source
receipt. Continuation wakes carry the exact facts before the atomic job update.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import _wait_clock
from ._base_runs import after_run, base_of, record_samples, with_base
from ._comparison import (
    as_answer,
    moment_of,
    run_reading,
)
from ._deadlines import resolve_until
from ._evaluation_history import produce_history
from ._measurement_reuse import (
    checkpoint_input,
    held_answer,
    held_input_run,
    reused_evidence_complete,
    reused_record,
    reused_run,
)
from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    Verdict,
    accepted_receipt_for,
    committed_stop,
    continues_admitted_work,
    current_closure,
    fresh_observation,
    observation_for,
    operation_for,
    released_owner,
    request_matches,
    stop_mode,
    submission_budget_for,
)
from ._registry import ContractError
from ._runaway import runaway_noted
from ._submission_receipts import (
    canonical_failure,
    conclusive,
    ended_unobserved,
    job_budget,
    submission_receipt,
)
from ._submissions import (
    Origin,
    allocate,
    capped,
    current,
    identity_of,
    rejected,
    submission_released,
)
from ._turn_permit import _executing_turn, _Facts, _permit_for_turn
from .types.attempts import (
    AttemptEvaluationHistoryUpdated,
    CloseAttemptScope,
    SnapshotAndRetain,
)
from .types.common import (
    MAX_SUBMISSIONS_PER_MEASUREMENT,
    AttemptId,
    AttemptRef,
    CompletionStatus,
    EvidenceKind,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    RequestId,
    RevisionRef,
    RunStatus,
    Scope,
    StopMode,
)
from .types.evaluation import (
    MEASUREMENT_DIAGNOSTIC_LIMIT,
    AgentCall,
    AgentMeasurementRequested,
    AgentRejection,
    CancelOwnedJob,
    CollectEvidence,
    ContinuationJobsChanged,
    ContinuationPhase,
    EvidenceAcceptanceReceipt,
    InspectOwnedJob,
    JobObserved,
    JobQueued,
    JobsDrainRequested,
    JobTerminationRequested,
    JobWaitLapsed,
    MeasurementIdentity,
    MeasurementRequested,
    MeasurementResult,
    MeasurementSubmissionEnded,
    MeasurementSubmissionObserved,
    ObservationsDue,
    ObservedJobFacts,
    ObserveOwnedJob,
    ObservePacing,
    OwnedJob,
    PreparedSubmissionReceipt,
    RegisteredJobObserved,
    RegisteredJobRequested,
    RegisteredOwnedJob,
    RunJobsCancelRequested,
    SnapshotResultRef,
    SubmissionBudget,
    SubmitMeasurement,
    UnobservedJobFacts,
)
from .types.evaluation_history import EvaluationStageOutcome
from .types.job_observations import MeasurementFailure
from .types.kernel import AreaChange, DecisionCompleted
from .types.records import (
    Attribution,
    Outbound,
    RequestObserved,
    RequestPhase,
    inherit,
    within,
)
from .types.strategy import Measure
from .types.until import Until, Within
from .types.waits import WaitOn

if TYPE_CHECKING:
    from .types.common import Observation, ResourceId, Seconds
    from .types.evaluation import EvaluationEvent, EvaluationState, EvidenceRef, MeasurementPlan
    from .types.kernel import EvaluationContext, RunState, Signal
    from .types.records import RecordsState, Request, RequestRecord

__all__ = ["advance", "cancel_settled", "submission_ended"]


def _origin(context: EvaluationContext, event: MeasurementRequested) -> Verdict[Measure]:
    rows = tuple(
        receipt
        for receipt in context.run.receipts
        if isinstance(receipt.decision, Measure)
        and receipt.decision_id == event.decision_id
        and receipt.decision.scope == event.scope
        and receipt.decision.plan == event.plan
    )
    if not rows:
        return Missing(ProofReason.ABSENT_RECEIPT)
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
    rows = context.records.find(candidate.request_id)
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
    if (
        latest.failure is None
        or not latest.failure.retryable
        or (
            observation.accepted
            and not isinstance(submission_released(state, context, latest), Proven)
        )
    ):
        return Missing(ProofReason.UNRESOLVED)
    if len(budget.receipts) >= min(budget.limit, MAX_SUBMISSIONS_PER_MEASUREMENT):
        return Missing(ProofReason.ABSENT_CHARGE)
    return Proven(budget)


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
    identity = identity_of(plan)
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
    source = context.records.record(evidence.source_request)
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
    """Admit a ``Measure`` decision; an answer core gives at once is addressed to the decision.

    A decision with no request of its own to wait on is answered here (refused, answered from
    evidence core holds, or from the run that already measured its input), and it completes
    with that answer, so the receipt says it was answered (`unanswered_measures`).
    """
    change = _admitted(state, context, event)
    results = tuple(
        item.model_copy(update={"decision_id": event.decision_id})
        if isinstance(item, MeasurementResult)
        and item.source_request is None
        and item.decision_id is None
        else item
        for item in change.events
    )
    answers = tuple(
        item
        for item in results
        if isinstance(item, MeasurementResult) and item.decision_id == event.decision_id
    )
    if not answers:
        return change
    status = (
        CompletionStatus.SUCCEEDED
        if answers[-1].status == ObservationStatus.SUCCEEDED
        else CompletionStatus.FAILED
    )
    done = DecisionCompleted(decision_id=event.decision_id, status=status)
    return change.model_copy(update={"events": results, "signals": (*change.signals, done)})


def _answered_from_held(
    state: EvaluationState,
    context: EvaluationContext,
    event: MeasurementRequested,
    plan: MeasurementPlan,
    identity: MeasurementIdentity,
) -> AreaChange[EvaluationState] | None:
    """The answer to ``event`` from what core already holds, or None when it must be measured.

    The input's own run answers a repeat of its baseline; evidence of a profile capture that
    is still valid answers the capture asked for again.
    """
    run = held_input_run(state, event.scope, identity)
    if run is not None:
        return AreaChange(state=state, events=(held_answer(state, run, event.decision_id),))
    reuse = _reused(state, context, event.scope, plan)
    if not reuse:
        return None
    result = MeasurementResult(
        scope=event.scope, evidence=reuse, status=ObservationStatus.SUCCEEDED
    )
    return AreaChange(state=state, events=(result,))


def _admitted(
    state: EvaluationState, context: EvaluationContext, event: MeasurementRequested
) -> AreaChange[EvaluationState]:
    authority = current(context, event.scope)
    origin = _origin(context, event)
    resolved = _resolved_plan(context, event.scope, event.plan)
    if (
        not isinstance(authority, Proven)
        or not isinstance(origin, Proven)
        or not isinstance(resolved, Proven)
    ):
        return rejected(state, event.scope)
    plan = checkpoint_input(context, event.scope, resolved.value)
    identity = identity_of(plan)
    if not isinstance(identity, Proven):
        return rejected(state, event.scope)
    held = _answered_from_held(state, context, event, plan, identity.value)
    if held is not None:
        return held
    # Replayed commands never allocate another ordinal, including changed timing.
    if any(
        isinstance(row.request, SubmitMeasurement)
        and row.request.decision_id == origin.value.decision_id
        for row in context.records.records
    ):
        return AreaChange(state=state)
    run = reused_run(state, event.scope, plan, identity.value, moment_of(context.run))
    reuses = None if run is None else run.submission_id
    base = base_of(context, event.scope, plan) if event.compared else None
    origin_of = Origin(event.scope, origin.value.decision_id, reuses=reuses, base=base)
    change = _admit(state, context, origin_of, plan, identity.value)
    outbound = next((r for r in change.requests if isinstance(r.request, SubmitMeasurement)), None)
    if outbound is None or outbound.request.request_id is None:
        return change
    return with_base(change, context, origin_of, plan, outbound.request.request_id)


def _admit(
    state: EvaluationState,
    context: EvaluationContext,
    origin: Origin,
    plan: MeasurementPlan,
    identity: MeasurementIdentity,
) -> AreaChange[EvaluationState]:
    """Charge the identity's budget and allocate one submission, or reject."""
    scope = origin.scope
    matches = tuple(
        b for b in state.submission_budgets if b.scope == scope and b.identity == identity
    )
    budget = matches[0] if matches else None
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
        return rejected(state, scope, failure)
    return allocate(state, context, origin, plan, budget)


def _agent_requested(
    state: EvaluationState, context: EvaluationContext, event: AgentMeasurementRequested
) -> AreaChange[EvaluationState]:
    """Admit an agent tool call: same authority, identity and budget as a Measure.

    A call for a candidate this scope already measures or measured (same identity) is a
    repeat. It is answered with that submission and charges nothing: the scope's budget
    receipts are the one record of "this candidate's evaluation", so every role that holds
    the scope (an implementer and the judge reviewing its candidate) gets the same handle.
    A repeat exists only while the earlier submission cannot be redone; after a released
    retryable infrastructure failure the call is a real retry and is charged like any other.

    An agent's call is answered to the agent and tells the strategy nothing: a refusal of
    it is not the rejection of a Measure decision, and neither is the end of its job.
    """
    if any(call.call_id == event.call_id for call in state.agent_calls):
        return AreaChange(state=state)
    identity = identity_of(event.plan)
    facts = _Facts(context.run, context.sessions, context.records, state)
    requester = _executing_turn(context.sessions, event.scope)
    permit = _permit_for_turn(facts, requester, live=True)
    rejection: AgentRejection | None = None
    if not continues_admitted_work(context.run) and not (
        context.run.status == RunStatus.DRAINING and permit is not None
    ):
        rejection = AgentRejection.RUN_STOPPING
    elif not isinstance(current(context, event.scope), Proven):
        rejection = AgentRejection.NOT_ADMITTED
    elif not isinstance(identity, Proven):
        rejection = AgentRejection.INVALID_PLAN
    if rejection is not None or not isinstance(identity, Proven):
        return _recorded(AreaChange(state=state), AgentCall.refused(event, rejection))
    repeated = _repeated(state, context, event.scope, identity.value)
    if repeated is not None:
        request_id, deadline_at = repeated
        call = AgentCall.answered(event, request_id, deadline_at, repeat=True)
        return _recorded(AreaChange(state=state), call)
    origin = Origin(
        event.scope,
        None,
        capped(
            event,
            deadline_at=None if permit is None else permit.deadline_at,
            now_at=context.run.now_at,
        ),
        base=base_of(context, event.scope, event.plan),
        requested_by=requester,
    )
    change = _admit(state, context, origin, event.plan, identity.value)
    outbound = next((r for r in change.requests if isinstance(r.request, SubmitMeasurement)), None)
    if outbound is None or outbound.request.request_id is None:
        refused = AgentCall.refused(event, AgentRejection.NOT_ALLOWED)
        return _recorded(AreaChange(state=state), refused)
    call = AgentCall.answered(
        event, outbound.request.request_id, _deadline_at(context, outbound), repeat=False
    )
    based = with_base(change, context, origin, event.plan, outbound.request.request_id)
    recorded = _recorded(based, call)
    return recorded.model_copy(
        update={"state": runaway_noted(recorded.state, context, event.scope)}
    )


def _deadline_at(context: EvaluationContext, outbound: Outbound) -> Seconds:
    """The absolute time core ends a submission, for the agent that asked for it."""
    match outbound.until:
        case Until() as until:
            return until.at
        case Within() as within:
            return resolve_until(context.run, within).at
        case _:
            raise ContractError(("until",), "an agent submission has an explicit limit")


def _recorded(change: AreaChange[EvaluationState], call: AgentCall) -> AreaChange[EvaluationState]:
    """``change`` without the events meant for a strategy, and with ``call`` on record."""
    state = change.state
    return change.model_copy(
        update={
            "state": state.model_copy(update={"agent_calls": (*state.agent_calls, call)}),
            "events": (),
        }
    )


def _repeated(
    state: EvaluationState, context: EvaluationContext, scope: Scope, identity: MeasurementIdentity
) -> tuple[RequestId, Seconds] | None:
    """The submission a repeat call is answered with, and its deadline; None for a new one.

    The scope's budget for the identity holds the evaluation: its latest receipt is the
    submission that is running, or ended and cannot be redone. It is not a repeat when
    the budget would admit another submission (none yet, or a released retryable
    failure), nor when the receipt names no request (a migrated one).
    """
    matches = tuple(
        b for b in state.submission_budgets if b.scope == scope and b.identity == identity
    )
    if len(matches) != 1 or not matches[0].receipts:
        return None
    if isinstance(_budget_ready(state, context, matches[0]), Proven):
        return None
    latest = matches[0].receipts[-1]
    if not isinstance(latest, PreparedSubmissionReceipt):
        return None
    deadline_at = _deadline_of(state, context, latest.request_id)
    return None if deadline_at is None else (latest.request_id, deadline_at)


def _deadline_of(
    state: EvaluationState, context: EvaluationContext, request_id: RequestId
) -> Seconds | None:
    """The deadline core gave a submission, from the call that allocated it or its record."""
    call = next(
        (
            c
            for c in state.agent_calls
            if c.request_id == request_id and not c.repeat and c.deadline_at is not None
        ),
        None,
    )
    if call is not None:
        return call.deadline_at
    row = context.records.record(request_id)
    if row is not None and isinstance(row.request, SubmitMeasurement):
        return row.until.at
    return None


def _reported_to_strategy(context: EvaluationContext, job: OwnedJob | RegisteredOwnedJob) -> bool:
    """Whether the end of ``job`` is a result the strategy asked for.

    A strategy awaits the result of its own Measure decision and takes any result of its
    scope for that one. A job an agent submitted from its turn (a request without a
    decision) answers the agent, which waits for it through its continuation; reporting
    it too would hand the awaiting Measure another submission's evidence.
    """
    if isinstance(job, RegisteredOwnedJob):
        return True
    row = context.records.record(job.submission_id)
    return not (
        row is not None
        and isinstance(row.request, SubmitMeasurement)
        and row.request.decision_id is None
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
) -> Verdict[RequestRecord]:
    """The committed request an observation belongs to.

    A job's observations carry its submission's request id, but only the submission's
    own observation goes through the record ledger. With ``later``, the ledger must
    hold the submission's observation, and the incoming one may be newer than it: an
    earlier or conflicting one is refused, and freshness against the job follows.
    """
    rows = context.records.find(observation.request_id)
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
    """Apply a submission's own observation, then re-derive the attempt's history.

    The ledger takes a job's end as its submission's own observation after the job event
    that carried it, so the history that event produced could not yet read the closed
    submission. This is the first moment it can.
    """
    change = _submission_receipted(state, context, event)
    proof = _source(context, event.observation)
    if (
        not conclusive(event.observation)
        or not isinstance(proof, Proven)
        or not isinstance(proof.value.request, SubmitMeasurement)
    ):
        return change
    # A submission can end without a job to observe (refused at submission), and its end is
    # what frees a run its comparisons wait on, so they are re-checked here too.
    change = after_run(change, context, proof.value.request_id)
    scope = proof.value.request.scope
    wake = _deferred_wake(change.state, event, proof.value)
    # A waiting continuation derives and publishes the history itself when it authorizes.
    history = () if wake else _history_signals(change.state, context, scope)
    return change.model_copy(update={"signals": (*change.signals, *history, *wake)})


def submission_ended(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Close the budget entry of a submission core ended before it named a job, and re-check.

    No observation will reach the entry (its submission hung, or a restarted host could not
    find it), so left open it would count its run under way for good: every comparison of its
    input would stay settling and take no further run. A submission whose job is known ends
    with that job. The comparisons waiting on its input take the runs they still need
    (`after_run`).
    """
    if not isinstance(event, MeasurementSubmissionEnded):
        raise ContractError(("event", event.kind), "not a submission's end")
    if any(job.submission_id == event.request_id for job in state.jobs):
        return AreaChange(state=state)
    budgets = tuple(
        ended_unobserved(budget, event.request_id, event.reason)
        for budget in state.submission_budgets
    )
    if budgets == state.submission_budgets:
        return AreaChange(state=state)
    change = AreaChange(state=state.model_copy(update={"submission_budgets": budgets}))
    return after_run(change, context, event.request_id)


def _deferred_wake(
    state: EvaluationState, event: MeasurementSubmissionObserved, source: RequestRecord
) -> tuple[ContinuationJobsChanged, ...]:
    """Wake the scope's waiting continuations when one of its submissions concludes.

    A continuation's authorization waits for the scope to be settled (every submission
    concluded), so the conclusion of any submission of the scope, with or without a job, is
    a moment it can proceed. Each wake names one of the continuation's own jobs at its
    current observation, which the continuation handler accepts as its own job event.
    """
    if not conclusive(event.observation):
        return ()
    wakes: list[ContinuationJobsChanged] = []
    for continuation in state.continuations:
        if continuation.phase != ContinuationPhase.WAITING:
            continue
        job = next(
            (
                j
                for j in (*state.jobs, *state.registered_jobs)
                if j.resource_id in continuation.jobs
                and j.scope == source.request.scope
                and j.observation is not None
            ),
            None,
        )
        if job is None or job.resource_id is None or job.observation is None:
            continue
        wakes.append(
            ContinuationJobsChanged(
                resource_id=job.resource_id,
                observation=job.observation,
                previous=UnobservedJobFacts(resource_id=job.resource_id),
            )
        )
    return tuple(wakes)


def _awaited(state: EvaluationState, resource_id: ResourceId) -> bool:
    """Whether a waiting continuation names the job, so its authorization publishes the history."""
    return any(
        c.phase == ContinuationPhase.WAITING and resource_id in c.jobs for c in state.continuations
    )


def _submission_receipted(
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
    updated = submission_receipt(budget.value, event, source)
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


def _submission_job(
    state: EvaluationState,
    context: EvaluationContext,
    event: MeasurementSubmissionObserved,
    source: RequestRecord,
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
            reuses=request.reuses,
            base=request.base,
            status=ObservationStatus.PENDING,
            # A job nobody has polled is due now. Normally the first JobObserved replaces
            # this; after a restart that lost it (the submit ran, its job observation was
            # never committed) this is the only thing that starts the observe cycle.
            wait=WaitOn(timer=context.run.now_at),
        )
        state = state.model_copy(update={"jobs": (*state.jobs, job)})
        return AreaChange(state=state)
    if observation.terminal and not observation.accepted and request.decision_id is not None:
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
    authority = current(context, request.scope)
    if not isinstance(proof, Proven) or not isinstance(authority, Proven):
        return rejected(state, request.scope)
    if (
        request.operation.schema_ref.lifecycle != LifecycleClass.OWNED_JOB
        or request.request_id is None
    ):
        return rejected(state, request.scope)
    identity = proof.value.registered_measurement
    if event.expected_measurement != identity:
        return rejected(state, request.scope)
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
            else rejected(state, request.scope)
        )
    budgets = state.submission_budgets
    if identity is not None:
        matches = tuple(b for b in budgets if b.scope == request.scope and b.identity == identity)
        if matches:
            return rejected(state, request.scope)
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
        # Its operation's answer names the job; until then that request is what moves it.
        wait=WaitOn(requests=(request.request_id,)),
    )
    return AreaChange(
        state=state.model_copy(
            update={"registered_jobs": (*state.registered_jobs, job), "submission_budgets": budgets}
        ),
        requests=(inherit(request),),
    )


def _job_identity(job: OwnedJob | RegisteredOwnedJob) -> Verdict[MeasurementIdentity]:
    if isinstance(job, OwnedJob):
        return identity_of(job.plan)
    return (
        Proven(job.expected_measurement)
        if job.expected_measurement is not None
        else Missing(ProofReason.ABSENT_DECLARATION)
    )


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
    gated = evidence.kind in identity.accuracy_gated
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
    source: RequestRecord,
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


def _measure_origin(
    state: EvaluationState,
    context: EvaluationContext,
    source: RequestRecord,
    request: SubmitMeasurement,
) -> Verdict[None]:
    """Prove where a submission came from: an accepted Measure, or a recorded agent call.

    An agent call has no decision. Core's own record of the call it admitted names the
    request it allocated, so the same scope and request id prove the origin.
    """
    if request.decision_id is None:
        admitted = state.asked_without_decision(source.request_id, request.scope)
        return Proven(None) if admitted else Missing(ProofReason.ABSENT_RECEIPT)
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
    # Core set the measured input at admission from the scope's first checkpoint, if any then.
    if request.plan not in (
        resolved.value,
        checkpoint_input(context, request.scope, resolved.value),
    ):
        return Mismatch(ProofField.PAYLOAD)
    return Proven(None)


def _builtin_source(
    state: EvaluationState, context: EvaluationContext, job: OwnedJob, source: RequestRecord
) -> Verdict[OwnedJob]:
    request = source.request
    if not isinstance(request, SubmitMeasurement) or request.plan != job.plan:
        return Mismatch(ProofField.PAYLOAD)
    origin = _measure_origin(state, context, source, request)
    if not isinstance(origin, Proven):
        return origin
    budget = submission_budget_for(request, state.submission_budgets, context.run.receipts)
    return Proven(job) if isinstance(budget, Proven) else budget


def _incoming_job(
    job: OwnedJob | RegisteredOwnedJob,
    source: RequestRecord,
    event: JobObserved | RegisteredJobObserved,
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


@dataclass(frozen=True, slots=True)
class _Stored:
    """What a job's observation added to the ledger."""

    accepted: tuple[EvidenceRef, ...]
    """The job's records, as the ledger holds them."""
    ledger: tuple[EvidenceRef, ...]
    refused: bool
    """A repeat of a key came with different content."""
    measured: tuple[EvidenceRef, ...]
    """The records new to the ledger as the run reported them, so its samples read the run's
    own readings and not the answer an official record states (`as_answer`)."""


def _store_evidence(
    state: EvaluationState,
    job: OwnedJob | RegisteredOwnedJob,
    event: JobObserved | RegisteredJobObserved,
    now_at: Seconds,
) -> _Stored:
    """Accept evidence keyed by (source request, evidence id).

    The same EvidenceId from two jobs is two records. A repeat of one key with
    different content is refused, and the caller reports that refusal instead of
    dropping it silently. An official job's benchmark record enters the ledger as the
    measurement's answer (`as_answer`), and a repeat is compared in that form.
    """
    accepted = list(job.evidence)
    ledger = list(state.evidence)
    measured: list[EvidenceRef] = []
    refused = False
    for evidence in event.evidence:
        verdict = _evidence(job, event, evidence)
        if not isinstance(verdict, Proven) or not reused_record(state, job, evidence):
            continue
        reported = verdict.value
        incoming = (
            as_answer(state, job, reported, now_at) if isinstance(job, OwnedJob) else reported
        )
        previous = next((e for e in ledger if e.key == incoming.key), None)
        if previous is None:
            ledger.append(incoming)
            accepted.append(incoming)
            measured.append(reported)
        elif previous != incoming and not _same_report(state, job, previous, reported):
            refused = True
        elif incoming not in accepted:
            accepted.append(incoming)
            measured.append(reported)
    return _Stored(tuple(accepted), tuple(ledger), refused, tuple(measured))


def _same_report(
    state: EvaluationState,
    job: OwnedJob | RegisteredOwnedJob,
    previous: EvidenceRef,
    reported: EvidenceRef,
) -> bool:
    """Whether ``reported`` repeats the record the ledger holds as ``previous``, as its run read it.

    An official answer whose restating closed (``OwnedJob.answer_until``) keeps the mean it
    stated then, while `as_answer` would state the mean of every sample now; the run's own
    reading (`run_reading`) is what a repeat of its report must match.
    """
    return isinstance(job, OwnedJob) and (
        previous.model_copy(update={"reading": run_reading(state, job, previous)}) == reported
    )


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
    stored = _store_evidence(state, job, event, context.run.now_at)
    accepted, ledger, refused, measured = (
        stored.accepted,
        stored.ledger,
        stored.refused,
        stored.measured,
    )
    incomplete = not reused_evidence_complete(
        state, job, accepted, terminal=event.observation.terminal
    )
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
            "queue_credit": _wait_clock.credited(
                job, observation, context.run.limits, context.records.hosts
            ),
            **_next_poll(job, observation, context),
        }
    )
    collect = _collection(updated, context)
    if isinstance(updated, OwnedJob):
        updated = updated.model_copy(update={"collecting": _collecting(updated, context, collect)})
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
    claim = canonical_failure(
        observation,
        event.failure if isinstance(event, JobObserved) else None,
        has_facts=event.evaluation_result is not None,
    )
    state = job_budget(state, context, updated, claim, reuse_incomplete=incomplete)
    ran = record_samples(state, context, updated, measured, observation.observed_at)
    state, requests = ran.state, ran.requests
    wake = ContinuationJobsChanged(
        resource_id=observation.resource_id, observation=observation, previous=previous
    )
    events: tuple[MeasurementResult | JobQueued, ...] = _queued_notice(updated, job, context)
    source_id = updated.submission_id if isinstance(updated, OwnedJob) else updated.request_id
    reported = _reported_to_strategy(context, job)
    if conclusive(observation):
        newly = tuple(e for e in accepted if e not in job.evidence)
        # A refused evidence id is reported as an unclassified failure, never hidden.
        # A reused result short of its source's records says nothing of the candidate, so it
        # is the machinery's failure and the budget admits one fresh measurement.
        failure = (
            MeasurementFailure.UNKNOWN
            if refused
            else MeasurementFailure.INFRASTRUCTURE
            if incomplete
            else claim
        )
        if reported and not conclusive(job.observation):
            events = (
                *events,
                MeasurementResult(
                    scope=job.scope,
                    source_request=source_id,
                    evidence=tuple(accepted),
                    status=observation.status,
                    failure=failure,
                    diagnostic=observation.diagnostic[-MEASUREMENT_DIAGNOSTIC_LIMIT:],
                    operator_diagnostic=event.operator_diagnostic[-MEASUREMENT_DIAGNOSTIC_LIMIT:],
                ),
            )
        elif reported and (newly or refused or incomplete):
            # Late evidence is published once, as the delta, never replayed or dropped.
            events = (
                *events,
                MeasurementResult(
                    scope=job.scope,
                    source_request=source_id,
                    evidence=newly,
                    status=observation.status,
                    failure=failure,
                    diagnostic=observation.diagnostic[-MEASUREMENT_DIAGNOSTIC_LIMIT:],
                    operator_diagnostic=event.operator_diagnostic[-MEASUREMENT_DIAGNOSTIC_LIMIT:],
                ),
            )
        if collect is not None:
            requests = (*requests, collect)
    closing = _ended(context, job, event)
    return AreaChange(
        state=state,
        # While the ledger still has to take the job's end as its submission's own, the
        # history and the wake wait for it (see `_submission_observed`): both would read a
        # history in which this submission is not closed.
        signals=closing or (*_history_unless_awaited(state, context, job, event), wake),
        requests=requests if context.run.status != RunStatus.TERMINAL else (),
        # A comparison the run's end settled is told after the run's own result.
        events=(*events, *ran.events),
    )


def _collection(job: OwnedJob | RegisteredOwnedJob, context: EvaluationContext) -> Outbound | None:
    """The collection of an ended job's evidence to ask for now, if the job needs one.

    A cancel stop discards its run's jobs, so it collects nothing from them: a job the executor
    holds no record of cannot be collected from, and asking would hold the run closing for
    good. One collection per job: a collection that yields nothing (a job that failed before
    producing evidence) is itself observed as a job event, and asking again on that event
    would never end.
    """
    observation = job.observation
    if (
        not conclusive(observation)
        or observation is None
        or not observation.accepted
        or job.evidence
        or _cancelling_scope(context) == job.scope
    ):
        return None
    collect = _job_request(CollectEvidence, job, context, "evidence")
    if context.records.positions(collect.request_id):
        return None
    return _job_outbound(CollectEvidence, job, context, "evidence")


def _collecting(
    job: OwnedJob, context: EvaluationContext, collect: Outbound | None
) -> RequestId | None:
    """The collection of ``job``'s evidence still under way, while it may still bring a sample.

    The one asked for now, else the one asked for before while it is open and no evidence came.
    Only a framework run that succeeded can give a sample (`sample_of`): the evidence of any
    other changes nothing a comparison waits on, so its end is decided at once.
    """
    if (
        job.evidence
        or not job.terminal
        or job.status != ObservationStatus.SUCCEEDED
        or job.reuses is not None
    ):
        return None
    if collect is not None:
        return collect.request.request_id
    held = None if job.collecting is None else context.records.record(job.collecting)
    return job.collecting if held is not None and _open(held) else None


def _queued_notice(
    updated: OwnedJob | RegisteredOwnedJob,
    job: OwnedJob | RegisteredOwnedJob,
    context: EvaluationContext,
) -> tuple[JobQueued, ...]:
    """A ``JobQueued`` report each time a pending job's queue credit passes another interval.

    It carries the executor's pending reason as given, or none when the executor gave none.
    """
    limits = context.run.limits
    if updated.resource_id is None or not _wait_clock.notice_due(updated, job.queue_credit, limits):
        return ()
    progress = updated.progress
    return (
        JobQueued(
            scope=updated.scope,
            resource_id=updated.resource_id,
            source_request=_wait_clock.source(updated),
            queued_s=updated.queue_credit,
            budget_left_s=_wait_clock.queue_budget_left(updated, limits),
            pending_reason=None if progress is None else progress.pending_reason,
        ),
    )


def _ended(
    context: EvaluationContext,
    job: OwnedJob | RegisteredOwnedJob,
    event: JobObserved | RegisteredJobObserved,
) -> tuple[Signal, ...]:
    """The job's first conclusive observation, handed to the ledger as its submission's own.

    A job's observations carry its submission's request id and sequence, so the end of
    the job is a fact about the submit request, with the terminal facts the history reads.
    When the submit's own view was taken
    while the job still ran, nothing else completes that record, and a closing run waits
    for every open one. A submission the ledger already closed needs only the job's release:
    one closed while its job still ran (blocked at its bound when the run closed) holds the
    run closing until that is proved. One the ledger never dispatched cannot have been
    observed.
    """
    if (
        not isinstance(job, OwnedJob)
        or not conclusive(event.observation)
        or conclusive(job.observation)
    ):
        return ()
    record = context.records.record(job.submission_id)
    if record is None or record.phase == RequestPhase.PREPARED:
        return ()
    if record.phase == RequestPhase.COMPLETED and (
        record.observation is None or record.observation.released or not event.observation.released
    ):
        return ()
    return (
        RequestObserved(observation=event.observation, evaluation_result=event.evaluation_result),
    )


def _next_poll(
    job: OwnedJob | RegisteredOwnedJob, observation: Observation, context: EvaluationContext
) -> dict[str, ObservePacing | WaitOn | None]:
    """The pacing and wait of *job* after it took *observation*.

    An ended job waits on nothing. A poll the executor could not answer (not accepted)
    is retried after a backoff (``_backed_off``); after any other observation the job
    waits one ``observe_interval`` from the run limits for its next poll.
    """
    if observation.terminal:
        return {"pacing": ObservePacing(), "wait": None}
    if not observation.accepted:
        return _backed_off(job, context)
    timer = context.run.now_at + context.run.limits.observe_interval
    return {"pacing": ObservePacing(), "wait": WaitOn(timer=timer)}


def _backed_off(
    job: OwnedJob | RegisteredOwnedJob, context: EvaluationContext
) -> dict[str, ObservePacing | WaitOn | None]:
    """The job waits for a poll after a delay that doubles with each poll that did not move it.

    It doubles from ``observe_interval`` up to ``observe_backoff_cap``, both run limits.
    """
    limits = context.run.limits
    retries = job.pacing.retries + 1
    delay = limits.observe_interval
    for _ in range(retries - 1):
        delay = min(delay * 2, limits.observe_backoff_cap)
        if delay == limits.observe_backoff_cap:
            break
    return {
        "pacing": ObservePacing(retries=retries),
        "wait": WaitOn(timer=context.run.now_at + delay),
    }


def _poll(job: OwnedJob | RegisteredOwnedJob, context: EvaluationContext) -> Outbound:
    """The next poll of *job*, under an identity no earlier poll used.

    A poll is named by the observation it follows, so a poll that ended without the job
    taking an answer (blocked at its bound, or refused) would share its successor's name;
    the successor then counts on from it.
    """
    outbound = _job_outbound(ObserveOwnedJob, job, context, "observe")
    base = outbound.request.request_id
    if base is None or not context.records.positions(base):
        return outbound
    lapsed = 1
    while context.records.positions(RequestId(root=f"{base.root}{_LAPSED}{lapsed}")):
        lapsed += 1
    identity = RequestId(root=f"{base.root}{_LAPSED}{lapsed}")
    return outbound.model_copy(
        update={"request": outbound.request.model_copy(update={"request_id": identity})}
    )


_LAPSED = ":b"
"""What a poll's name adds to its base, with a count, when an earlier poll took the base."""


def poll_family(request_id: RequestId) -> tuple[str, int] | None:
    """The job prefix and observation sequence a poll of an owned job is named by, else None.

    `_poll` names a poll ``{prefix}:{sequence}``, ``prefix`` naming the job and ``sequence``
    the observation the poll follows, with ``:b{count}`` added when earlier polls took that
    name. A job's observation sequence only grows, so a poll of a lower sequence than the
    job's newest poll is never named again. Any other request id (a continuation's poll,
    whose id core allocates from its revision) gives None.
    """
    root = request_id.root
    base, lapsed, count = root.rpartition(_LAPSED)
    if lapsed and count.isdecimal() and count.isascii():
        root = base
    prefix, _, sequence = root.rpartition(":")
    if not (
        prefix.startswith("measurement:")
        and prefix.endswith(":observe")
        and sequence.isdecimal()
        and sequence.isascii()
    ):
        return None
    return prefix, int(sequence)


def _poll_due(
    state: EvaluationState, context: EvaluationContext, event: ObservationsDue
) -> AreaChange[EvaluationState]:
    """Issue the poll of every job whose wait timer has come, once each; it then waits on it."""
    if context.run.status == RunStatus.TERMINAL:
        return AreaChange(state=state)
    requests: list[Outbound] = []

    def release[J: OwnedJob | RegisteredOwnedJob](job: J) -> J:
        wait = job.wait
        if wait is None or wait.timer is None or wait.timer > event.now_at:
            return job
        poll = _poll(job, context)
        identity = poll.request.request_id
        if identity is None:
            raise ContractError(("job", "poll"), "a poll is named by its job")
        requests.append(poll)
        return job.model_copy(update={"wait": WaitOn(requests=(identity,))})

    state = state.model_copy(
        update={
            "jobs": tuple(release(job) for job in state.jobs),
            "registered_jobs": tuple(release(job) for job in state.registered_jobs),
        }
    )
    return AreaChange(state=state, requests=tuple(requests))


def _job_wait_ended(
    state: EvaluationState, context: EvaluationContext, event: ObservationsDue | JobWaitLapsed
) -> AreaChange[EvaluationState]:
    """What a job's wait ends with: its poll timer came, or the request it named ended."""
    if isinstance(event, ObservationsDue):
        return _poll_due(state, context, event)
    return _wait_lapsed(state, context, event)


def _wait_lapsed(
    state: EvaluationState, context: EvaluationContext, event: JobWaitLapsed
) -> AreaChange[EvaluationState]:
    """A job whose poll ended without moving it polls again after its backoff.

    The poll blocked at its bound (the executor never answered in time, or the host was
    down past it) or the job refused its answer. A registered job whose operation ended
    without ever naming a job has nothing to poll: it ends with what that request reported,
    and its release is proved from that record like any other. An ended job whose evidence
    collection ended without reaching it stops collecting, and its run's end is decided then
    (`after_run`).
    """

    def woken[J: OwnedJob | RegisteredOwnedJob](job: J) -> J:
        if isinstance(job, OwnedJob) and job.collecting == event.request_id:
            return job.model_copy(update={"collecting": None})
        if _owner_request(job) != event.job or job.wait is None:
            return job
        ended = context.records.record(event.request_id)
        if event.request_id not in job.wait.requests or ended is None or _open(ended):
            return job
        if job.resource_id is not None:
            return job.model_copy(update=_backed_off(job, context))
        observation = ended.observation
        return job.model_copy(
            update={
                "status": ObservationStatus.UNKNOWN if observation is None else observation.status,
                "terminal": True,
                "released": observation is not None and observation.released,
                "wait": None,
            }
        )

    change = AreaChange(
        state=state.model_copy(
            update={
                "jobs": tuple(woken(job) for job in state.jobs),
                "registered_jobs": tuple(woken(job) for job in state.registered_jobs),
            }
        )
    )
    collected = any(
        job.submission_id == event.job and job.collecting == event.request_id for job in state.jobs
    )
    # The collection of an ended job's evidence ended without reaching it: its run gave no
    # sample, and the comparisons it serves take the runs they still need.
    return after_run(change, context, event.job) if collected else change


def _open(record: RequestRecord) -> bool:
    return record.phase in (
        RequestPhase.PREPARED,
        RequestPhase.DISPATCHED,
        RequestPhase.RECONCILING,
    )


def _history_unless_awaited(
    state: EvaluationState,
    context: EvaluationContext,
    job: OwnedJob | RegisteredOwnedJob,
    event: JobObserved | RegisteredJobObserved,
) -> tuple[AttemptEvaluationHistoryUpdated, ...]:
    """The history signal, unless the continuation this job's end wakes publishes it itself."""
    if (
        job.resource_id is not None
        and conclusive(event.observation)
        and _awaited(state, job.resource_id)
    ):
        return ()
    return _history_signals(state, context, job.scope)


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
    history = produce_history(scope, state, context.records, owner, context.run)
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


#: The step that ends a job the executor has not confirmed: an inspection, before any cancel.
_TERMINATION_INSPECT = "inspect"


def _step_prefix(job: OwnedJob | RegisteredOwnedJob, action: str) -> str:
    """The request id of *action* on *job*, without the observation sequence it may name."""
    source = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    return f"measurement:{source.root}:{action}"


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
    # Evidence is collected once per job, so its id does not name an observation.
    suffix = "" if model is CollectEvidence else f":{sequence}"
    if model is CancelOwnedJob:
        suffix += _resend_suffix(context, f"{_step_prefix(job, action)}{suffix}")
    canonical = context.records.record(source)
    # A step on a job is the job's own, whoever asks for it: a poll raised by the clock, a
    # stop, a drain and a late observation all name the submission's decision. Left to the
    # asker, two events asking for one step at one instant stamp it differently and the one
    # request id would carry two payloads.
    return model(
        request_id=RequestId(root=f"{_step_prefix(job, action)}{suffix}"),
        scope=job.scope,
        admission_id=canonical.request.admission_id if canonical is not None else None,
        decision_id=canonical.request.decision_id if canonical is not None else None,
        resource_id=job.resource_id,
    )


def _resend_suffix(context: EvaluationContext, base: str) -> str:
    """Name a cancel sent again for the same observation after earlier ones blocked.

    A cancel blocked at its bound proved nothing (``cancel_settled``), and the job may not be
    observed again before core asks once more (its submission ended, so nothing polls it).
    Each re-send gets its own id, so it is a new request and not the blocked one.
    """
    blocked = sum(
        1
        for row in context.records.records
        if (row.phase == RequestPhase.BLOCKED or row.failure is not None)
        and (row.request_id.root == base or row.request_id.root.startswith(f"{base}:resend-"))
    )
    return f":resend-{blocked}" if blocked else ""


def _job_outbound(
    model: type[ObserveOwnedJob]
    | type[InspectOwnedJob]
    | type[CancelOwnedJob]
    | type[CollectEvidence],
    job: OwnedJob | RegisteredOwnedJob,
    context: EvaluationContext,
    action: str,
) -> Outbound:
    """A job request with the bound its kind gets: cancellation or reconciliation.

    A step that names the submission's decision keeps it, an agent's ``None`` included: any
    signal of a step may ask for it, and the asker's decision is not the job's.
    """
    limits = context.run.limits
    bound = limits.cancellation_bound if model is CancelOwnedJob else limits.reconciliation_bound
    outbound = within(_job_request(model, job, context, action), bound)
    return outbound.model_copy(update={"attribution": Attribution.OWN})


def _terminate(
    state: EvaluationState,
    context: EvaluationContext,
    resource: ResourceId,
    *,
    cancel_unconfirmed: bool = False,
) -> AreaChange[EvaluationState]:
    rows = tuple(j for j in (*state.jobs, *state.registered_jobs) if j.resource_id == resource)
    if len(rows) != 1:
        return AreaChange(state=state)
    job = rows[0]
    if isinstance(released_owner(job, context.records.records), Proven):
        return AreaChange(state=state)
    source_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
    source = context.records.record(source_id)
    if (
        source is None
        or not isinstance(request_matches(source, source.request), Proven)
        or not isinstance(_job_source(state, context, job, source), Proven)
    ):
        return AreaChange(state=state)
    return _termination(
        state, context, _next_step(job, context, cancel_unconfirmed=cancel_unconfirmed)
    )


def _next_step(
    job: OwnedJob | RegisteredOwnedJob, context: EvaluationContext, *, cancel_unconfirmed: bool
) -> Outbound:
    """The one step that moves *job* toward its end, given what the executor has confirmed."""
    unconfirmed = (
        job.observation is None
        or job.observation.status == ObservationStatus.UNKNOWN
        or not job.observation.accepted
    )
    if unconfirmed and not (cancel_unconfirmed and job.observation is not None):
        return _job_outbound(InspectOwnedJob, job, context, _TERMINATION_INSPECT)
    if job.terminal:
        return _job_outbound(InspectOwnedJob, job, context, "release")
    return _job_outbound(CancelOwnedJob, job, context, "cancel")


def _termination(
    state: EvaluationState, context: EvaluationContext, outbound: Outbound
) -> AreaChange[EvaluationState]:
    """Ask for a termination step once: the request id names the job's step, not the asker.

    The stop, a drain, a termination request and a late observation can all want the same
    step at the same instant. Each would stamp it with its own decision, and one id may not
    carry two payloads, so a step already on the ledger is the job's one outstanding intent.
    """
    request_id = outbound.request.request_id
    if context.records.positions(request_id):
        return AreaChange(state=state)
    return AreaChange(state=state, requests=(outbound,))


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
    authority = context.records.record(event.authority)
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


def _cancelling_scope(context: EvaluationContext) -> Scope | None:
    """The scope whose jobs a closing run cancels: under a cancel stop, or past its deadline.

    A drain lets the run's own jobs finish, but only while the run has time left. From the
    run deadline on nothing is waited for, as for an attempt's queued episodes (scheduling
    retires them with a cancel then), so a job that never ends cannot hold the run closing.
    """
    match committed_stop(context.run):
        case Proven(value=stop) if context.run.status == RunStatus.CLOSING and (
            stop_mode(context.run) is StopMode.CANCEL
            or context.run.now_at >= context.run.deadline_at
        ):
            return stop.scope
        case _:
            return None


def _cancel_requested(context: EvaluationContext, resource: ResourceId) -> bool:
    """Whether core is done asking to cancel *resource* (see ``cancel_settled``)."""
    return cancel_settled(context.records, context.run, resource)


def cancel_settled(records: RecordsState, run: RunState, resource: ResourceId) -> bool:
    """Whether core is done asking to cancel *resource*: one cancel is open or was answered.

    A cancel blocked at its bound proved nothing: the executor's cancel failed or its outcome
    is unknown, and the job may still run. It still proves nothing once a closing run settles
    it (``failure`` keeps why core ended it without a result). It does not count, so core
    asks for a new cancel (on the job's next observation, or as the blocked cancel ends when
    nothing polls the job) instead of leaving the job to end by itself. The backend's
    ``request_retries.limit`` bounds those re-sends; past it core waits for the job's own
    end, as it did before any retry.
    """
    cancels = tuple(
        row
        for row in records.about_resource(resource)
        if isinstance(row.request, CancelOwnedJob) and not row.request.for_park
    )
    blocked = sum(
        1 for row in cancels if row.phase == RequestPhase.BLOCKED or row.failure is not None
    )
    return blocked < len(cancels) or blocked > run.limits.request_retries.limit


def _known(job: OwnedJob | RegisteredOwnedJob) -> bool:
    """Whether the executor has confirmed the job, so that the next step is a cancellation."""
    return (
        job.observation is not None
        and job.observation.accepted
        and job.observation.status != ObservationStatus.UNKNOWN
    )


def _inspected(context: EvaluationContext, resource: ResourceId) -> bool:
    return any(
        isinstance(row.request, InspectOwnedJob) for row in context.records.about_resource(resource)
    )


def _termination_inspected(context: EvaluationContext, job: OwnedJob | RegisteredOwnedJob) -> bool:
    """Whether core already asked to end *job* and, finding it unconfirmed, inspected it."""
    if job.resource_id is None:
        return False
    prefix = f"{_step_prefix(job, _TERMINATION_INSPECT)}:"
    return any(
        isinstance(row.request, InspectOwnedJob) and row.request_id.root.startswith(prefix)
        for row in context.records.about_resource(job.resource_id)
    )


def _closing_scopes(context: EvaluationContext) -> frozenset[Scope]:
    """The scopes of the attempts whose closure is proved: closing drains their jobs."""
    return frozenset(
        Scope(owner=owner.attempt_id, generation=owner.generation)
        for owner in context.attempts.attempts
        if owner.closure is not None and isinstance(current_closure(owner, owner.closure), Proven)
    )


def _cancel_scope_jobs(
    state: EvaluationState, context: EvaluationContext, scope: Scope | None
) -> AreaChange[EvaluationState]:
    """Move every live job core is ending one step on, at most one cancellation per job.

    Core ends every job of an attempt whose closure is proved (the drain asks once, when
    the closure is requested, and may find the close request not yet on the ledger), every
    job it already asked to terminate (a continuation's deadline, a retirement), and every
    job *scope* owns: the run's own jobs (its input baseline) have no attempt to close, so
    ``_cancelling_scope`` ends them. Applied when a cancel stop commits and again as each
    job is observed or its poll falls due.

    A job the executor does not confirm cannot be cancelled by name, so core inspects it
    once. If it is still unconfirmed after that, whether the executor holds no record of it
    or cannot say, core cancels it anyway: a cancellation is idempotent, and the executor
    answers it for a job that never began exactly as for one it ended. Waiting for a
    confirmation that may never come would hold the run closing until its budget ran out.
    """
    closing = _closing_scopes(context)
    requests = tuple(
        request
        for job in (*state.jobs, *state.registered_jobs)
        if job.resource_id is not None
        and not job.terminal
        and not _cancel_requested(context, job.resource_id)
        and (job.scope == scope or job.scope in closing or _termination_inspected(context, job))
        for request in _terminate(
            state, context, job.resource_id, cancel_unconfirmed=_inspected(context, job.resource_id)
        ).requests
    )
    return AreaChange(state=state, requests=requests)


def _run_jobs_cancel(
    state: EvaluationState, context: EvaluationContext, event: RunJobsCancelRequested
) -> AreaChange[EvaluationState]:
    match committed_stop(context.run):
        case Proven(value=stop) if stop.decision_id == event.authority:
            pass
        case _:
            return AreaChange(state=state)
    if _cancelling_scope(context) != event.scope:
        return AreaChange(state=state)
    return _cancel_scope_jobs(state, context, event.scope)


def _cancelled_after_stop(
    change: AreaChange[EvaluationState], context: EvaluationContext
) -> AreaChange[EvaluationState]:
    """Keep ending the jobs core is ending as they are observed (see ``_cancel_scope_jobs``)."""
    if context.run.status == RunStatus.TERMINAL:
        return change
    cancels = _cancel_scope_jobs(change.state, context, _cancelling_scope(context))
    if not cancels.requests:
        return change
    return change.model_copy(update={"requests": (*change.requests, *cancels.requests)})


def advance(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Preserve sibling continuations while consuming every measurement event."""
    match event:
        case MeasurementRequested():
            change = _requested(state, context, event)
        case AgentMeasurementRequested():
            change = _agent_requested(state, context, event)
        case MeasurementSubmissionObserved():
            change = _cancelled_after_stop(_submission_observed(state, context, event), context)
        case RegisteredJobRequested():
            change = _registered_requested(state, context, event)
        case JobObserved() | RegisteredJobObserved():
            change = _cancelled_after_stop(_job_observed(state, context, event), context)
        case ObservationsDue() | JobWaitLapsed():
            change = _cancelled_after_stop(_job_wait_ended(state, context, event), context)
        case JobTerminationRequested():
            change = _terminate(state, context, event.resource_id)
        case JobsDrainRequested():
            change = _drain(state, context, event)
        case RunJobsCancelRequested():
            change = _run_jobs_cancel(state, context, event)
        case _:
            raise ContractError(("event", event.kind), "not a measurement event")
    return change
