"""Pure wait-all authorization, frozen deadlines and guarded scope reopening.

CONT-BOUND remains a cutover prerequisite: scientific repeated-failure and
no-new-evaluation policy must consume the durable history and publication bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ._evaluation_history import produce_history
from ._registry import ContractError
from .types.attempts import (
    AttemptEvaluationHistoryUpdated,
    AttemptPhase,
    CloseAttemptScope,
    ScopeReopenRequested,
)
from .types.common import (
    AttemptRef,
    CompletionStatus,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    OperationId,
    OperationNormalizationKind,
    RequestId,
    RunStatus,
    Scope,
)
from .types.evaluation import (
    Continuation,
    ContinuationJobsChanged,
    ContinuationPhase,
    ContinuationReopenRequested,
    ContinuationRetireRequested,
    ContinuationScopeReopened,
    DeadlineReached,
    InspectOwnedJob,
    JobTerminationRequested,
    ObserveOwnedJob,
    RegisteredOwnedJob,
    ResumeAuthorizationReceipt,
    ResumeAuthorized,
    TurnSuspended,
)
from .types.evaluation_history import EvaluationHistoryAvailability
from .types.intents import InspectRequest, IntentPhase
from .types.job_observations import JobTimeout, TimedOut
from .types.kernel import AreaChange
from .types.scope_reopen import ScopedAdmissionReopenOutcome
from .types.sessions import Access, DispatchTurn, InspectTurn, ResumeSessionTurn, SessionPhase
from .types.strategy import Accepted, Operation

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import EvidenceKey, Observation, ResourceId
    from .types.evaluation import (
        EvaluationEvent,
        EvaluationState,
        EvidenceRef,
        OwnedJob,
    )
    from .types.evaluation_history import EvaluationHistoryCursor
    from .types.intents import ChildLease, Intent, Request
    from .types.kernel import EvaluationContext, Signal, StrategyEvent
    from .types.sessions import Invocation


def _invocation(context: EvaluationContext, continuation: Continuation) -> Invocation:
    invocation = next(
        (row for row in context.sessions.invocations if row.invocation == continuation.invocation),
        None,
    )
    if invocation is None:
        raise ContractError(("continuation", "invocation"), "requires an owned invocation")
    return invocation


def _attempt(context: EvaluationContext, scope: Scope) -> AttemptView | None:
    return next(
        (
            row
            for row in context.attempts.attempts
            if row.attempt_id == scope.owner and row.generation == scope.generation
        ),
        None,
    )


def _active(context: EvaluationContext, scope: Scope) -> bool:
    if context.run.status != RunStatus.RUNNING:
        return False
    if scope.owner == context.run.run_id:
        return scope.generation == context.run.generation
    attempt = _attempt(context, scope)
    return (
        attempt is not None
        and attempt.phase == AttemptPhase.ACTIVE
        and attempt.closure is None
        and attempt.admission_id is not None
    )


def _jobs(
    state: EvaluationState, continuation: Continuation
) -> tuple[OwnedJob | RegisteredOwnedJob, ...]:
    rows = (*state.jobs, *state.registered_jobs)
    owned: list[OwnedJob | RegisteredOwnedJob] = []
    for identity in continuation.jobs:
        matching = tuple(row for row in rows if row.resource_id == identity)
        if len(matching) != 1:
            raise ContractError(
                ("continuation", "jobs"), "dependency requires exactly one owned job"
            )
        owned.append(matching[0])
    return tuple(owned)


def _job_ownership(
    state: EvaluationState, context: EvaluationContext, continuation: Continuation
) -> None:
    if not continuation.jobs or len(set(continuation.jobs)) != len(continuation.jobs):
        raise ContractError(("continuation", "jobs"), "requires distinct nonempty dependencies")
    scope = _invocation(context, continuation).scope
    if any(job.scope != scope for job in _jobs(state, continuation)):
        raise ContractError(("continuation", "jobs"), "dependency belongs to a different scope")


def _resource(job: OwnedJob | RegisteredOwnedJob) -> ResourceId:
    if job.resource_id is None:
        raise ContractError(("continuation", "jobs"), "dependency has no external identity")
    return job.resource_id


def _settled(job: OwnedJob | RegisteredOwnedJob) -> bool:
    return job.terminal and job.status in (
        ObservationStatus.SUCCEEDED,
        ObservationStatus.FAILED,
        ObservationStatus.CANCELLED,
        ObservationStatus.REJECTED,
    )


def _feedback_evidence(records: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    unique: dict[EvidenceKey, EvidenceRef] = {}
    for evidence in records:
        previous = unique.get(evidence.key)
        if previous is not None and previous != evidence:
            raise ContractError(
                ("evidence", evidence.evidence_id.root), "conflicting evidence identity"
            )
        unique[evidence.key] = evidence
    return tuple(unique.values())


def _submission(job: OwnedJob | RegisteredOwnedJob) -> RequestId:
    return job.request_id if isinstance(job, RegisteredOwnedJob) else job.submission_id


def _release_complete(observation: Observation | None) -> bool:
    return (
        observation is not None
        and observation.terminal
        and observation.released
        and observation.children_complete
        and observation.status not in (ObservationStatus.PENDING, ObservationStatus.UNKNOWN)
    )


def _job_released(job: OwnedJob | RegisteredOwnedJob) -> bool:
    observation = job.observation
    return (
        _settled(job)
        and job.released
        and _release_complete(observation)
        and observation is not None
        and observation.request_id == _submission(job)
        and observation.scope == job.scope
        and observation.resource_id == job.resource_id
        and observation.status == job.status
    )


def _child_released(child: ChildLease) -> bool:
    observation = child.observation
    return (
        _release_complete(observation)
        and observation is not None
        and observation.resource_id == child.resource_id
        and observation.scope == child.scope
        and observation.request_id in child.source_requests
    )


def _descendant_released(
    state: EvaluationState, context: EvaluationContext, resource: ResourceId, scope: Scope
) -> bool:
    jobs = tuple(
        job for job in (*state.jobs, *state.registered_jobs) if job.resource_id == resource
    )
    leases = tuple(child for child in context.intents.children if child.resource_id == resource)
    return bool(jobs or leases) and (
        len(jobs) <= 1
        and len(leases) <= 1
        and all(job.scope == scope and _job_released(job) for job in jobs)
        and all(child.scope == scope and _child_released(child) for child in leases)
    )


def _descendants(
    state: EvaluationState,
    context: EvaluationContext,
    jobs: tuple[OwnedJob | RegisteredOwnedJob, ...],
    scope: Scope,
) -> set[ResourceId]:
    sources = {_submission(job) for job in jobs}
    resources = {_resource(job) for job in jobs}
    descendants = {child for job in jobs for child in job.children}
    descendants.update(
        child
        for job in jobs
        if job.observation is not None
        for child in job.observation.descendants
    )
    while True:
        previous = (len(resources), len(sources))
        resources.update(descendants)
        for job in (*state.jobs, *state.registered_jobs):
            if job.scope == scope and job.resource_id in resources:
                sources.add(_submission(job))
                descendants.update(job.children)
                if job.observation is not None:
                    descendants.update(job.observation.descendants)
        for child in context.intents.children:
            if child.scope == scope and (
                sources.intersection(child.source_requests)
                or resources.intersection(child.parent_resources)
            ):
                descendants.add(child.resource_id)
                if child.observation is not None:
                    descendants.update(child.observation.descendants)
        resources.update(descendants)
        if previous == (len(resources), len(sources)):
            break
    return descendants - {_resource(job) for job in jobs}


def _released_dependencies(
    state: EvaluationState, context: EvaluationContext, continuation: Continuation
) -> None:
    jobs = _jobs(state, continuation)
    if any(not _job_released(job) for job in jobs):
        raise ContractError(
            ("continuation", "jobs"),
            "reopening requires independent terminal release proof for every dependency",
        )
    scope = _invocation(context, continuation).scope
    if any(
        not _descendant_released(state, context, resource, scope)
        for resource in _descendants(state, context, jobs, scope)
    ):
        raise ContractError(
            ("continuation", "jobs", "children"),
            "reopening requires independent release proof for every discovered descendant",
        )


def _deadline_proof(state: EvaluationState, continuation: Continuation) -> None:
    if continuation.timeout is not None and (
        continuation.timeout.deadline_at != continuation.deadline_at
        or any(
            item.resource_id not in continuation.jobs for item in continuation.timeout.unfinished
        )
    ):
        raise ContractError(
            ("continuation", "timeout"),
            "frozen timeout must match its deadline and owned dependencies",
        )
    if continuation.timeout is None and any(
        job.observation is not None and job.observation.observed_at >= continuation.deadline_at
        for job in _jobs(state, continuation)
    ):
        # A post-update wakeup has lost the previous progress needed to freeze
        # before the observation. It cannot stand in for the missing proof.
        raise ContractError(
            ("continuation", "timeout"),
            "deadline must freeze before job facts; prior progress is unavailable",
        )


def _ready(state: EvaluationState, continuation: Continuation) -> bool:
    return continuation.timeout is not None or all(
        _settled(job) for job in _jobs(state, continuation)
    )


def _store(state: EvaluationState, continuation: Continuation) -> EvaluationState:
    return state.model_copy(
        update={
            "continuations": tuple(
                continuation if row.continuation_id == continuation.continuation_id else row
                for row in state.continuations
            )
        }
    )


def _authorize(
    state: EvaluationState,
    context: EvaluationContext,
    continuation: Continuation,
    *,
    reopened: bool = False,
) -> AreaChange[EvaluationState]:
    _job_ownership(state, context, continuation)
    if continuation.phase != ContinuationPhase.WAITING or not _ready(state, continuation):
        return AreaChange(state=state)
    _deadline_proof(state, continuation)
    invocation = _invocation(context, continuation)
    if not _active(context, invocation.scope):
        return AreaChange(state=state)
    _successor(context, continuation)
    _yield_proof(state, context, invocation, continuation, retained=reopened)
    if reopened:
        _released_dependencies(state, context, continuation)
    if any(job.scope != invocation.scope for job in _jobs(state, continuation)):
        raise ContractError(("continuation", "jobs"), "dependency belongs to a different scope")
    evidence = continuation.evidence
    if continuation.timeout is not None and _feedback_evidence(evidence) != evidence:
        raise ContractError(
            ("continuation", "evidence"), "stored timeout evidence must be canonical and immutable"
        )
    if continuation.timeout is None:
        evidence = _feedback_evidence(
            tuple(item for job in _jobs(state, continuation) for item in job.evidence)
        )
    if continuation.authorization_receipt is not None:
        return AreaChange(
            state=_store(
                state, continuation.model_copy(update={"phase": ContinuationPhase.AUTHORIZED})
            )
        )
    owner = _attempt(context, invocation.scope)
    history = (
        produce_history(invocation.scope, state, context.intents, owner, context.run)
        if owner
        else None
    )
    cursor = (
        history.cursor
        if history is not None and history.availability == EvaluationHistoryAvailability.COMPLETE
        else None
    )
    publication = ResumeAuthorizationReceipt(
        continuation_id=continuation.continuation_id,
        next_invocation=continuation.next_invocation,
        evidence=evidence,
        timeout=continuation.timeout,
        history_cursor=cursor,
    )
    authorized = continuation.model_copy(
        update={
            "phase": ContinuationPhase.AUTHORIZED,
            "evidence": evidence,
            "authorization_receipt": publication,
        }
    )
    return AreaChange(
        state=_store(state, authorized),
        events=(
            ResumeAuthorized(
                continuation_id=authorized.continuation_id,
                next_invocation=authorized.next_invocation,
                evidence=authorized.evidence,
                timeout=authorized.timeout,
                history_cursor=publication.history_cursor,
            ),
        ),
        signals=(
            AttemptEvaluationHistoryUpdated(
                attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                history=history,
            ),
        )
        if owner is not None and history is not None
        else (),
    )


def _successor(context: EvaluationContext, continuation: Continuation) -> None:
    current, successor = continuation.invocation, continuation.next_invocation
    if (
        current.session_id != successor.session_id
        or current.generation != successor.generation
        or current.invocation_id == successor.invocation_id
    ):
        raise ContractError(
            ("continuation", "next_invocation"), "requires a distinct canonical successor"
        )
    if any(row.invocation == successor for row in context.sessions.invocations):
        raise ContractError(
            ("continuation", "next_invocation"), "successor identity is already used"
        )


def _validate_new(
    state: EvaluationState, context: EvaluationContext, continuation: Continuation
) -> Invocation:
    if (
        continuation.phase != ContinuationPhase.WAITING
        or continuation.timeout is not None
        or continuation.evidence
        or continuation.park_authority is not None
        or continuation.reopen_authority is not None
        or continuation.cancelled_resolutions
        or continuation.authorization_receipt is not None
        or continuation.preceding_submission is not None
    ):
        raise ContractError(("continuation",), "new suspension must contain only waiting intent")
    if not continuation.jobs or len(set(continuation.jobs)) != len(continuation.jobs):
        raise ContractError(("continuation", "jobs"), "requires distinct nonempty dependencies")
    _successor(context, continuation)
    current = continuation.invocation
    invocation = _invocation(context, continuation)
    if (
        invocation.turn.invocation_id != current.invocation_id
        or invocation.turn.session.session_id != current.session_id
        or invocation.scope.generation != current.generation
        or not _active(context, invocation.scope)
    ):
        raise ContractError(("continuation", "invocation"), "requires current active ownership")
    if any(job.scope != invocation.scope for job in _jobs(state, continuation)):
        raise ContractError(("continuation", "jobs"), "dependency belongs to a different scope")
    if continuation.deadline_at > context.run.deadline_at:
        raise ContractError(("continuation", "deadline_at"), "exceeds run deadline")
    if any(
        row.invocation == current
        or row.next_invocation == continuation.next_invocation
        or (
            row.phase
            in (
                ContinuationPhase.WAITING,
                ContinuationPhase.PARKED,
                ContinuationPhase.REOPENING,
                ContinuationPhase.AUTHORIZED,
            )
            and row.next_invocation != current
            and _invocation(context, row).scope == invocation.scope
        )
        for row in state.continuations
    ):
        raise ContractError(
            ("continuation", "invocation"), "already owns an unfinished continuation"
        )
    return invocation


def _declared_operation(
    context: EvaluationContext,
    request: ExecuteRegisteredOperation,
    lifecycle: LifecycleClass,
    normalization: OperationNormalizationKind = OperationNormalizationKind.NONE,
) -> bool:
    schema = request.operation.schema_ref
    descriptors = tuple(
        descriptor
        for descriptor in context.run.capabilities.operations
        if descriptor.kind == schema.kind
    )
    if len(descriptors) != 1:
        return False
    descriptor = descriptors[0]
    return (
        descriptor.request_schema == schema.request_schema
        and descriptor.outcome_schema == schema.outcome_schema
        and descriptor.lifecycle == schema.lifecycle == lifecycle
        and descriptor.normalization == normalization
    )


def _turn_matches(context: EvaluationContext, invocation: Invocation, intent: Intent) -> bool:
    request = intent.request
    if intent.lifecycle != LifecycleClass.SESSION_TURN or request.request_id != intent.request_id:
        return False
    if request.scope != invocation.scope:
        return False
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return invocation.registered_operation is None and request.turn == invocation.turn
    if not isinstance(request, ExecuteRegisteredOperation):
        return False
    return (
        request.operation_id == invocation.registered_operation
        and _declared_operation(context, request, LifecycleClass.SESSION_TURN)
        and any(
            receipt.decision_id == request.decision_id
            and isinstance(receipt.feedback, Accepted)
            and receipt.feedback.decision_id == receipt.decision_id
            and receipt.decision is not None
            and receipt.decision.decision_id == receipt.decision_id
            and receipt.completion not in (CompletionStatus.FAILED, CompletionStatus.CANCELLED)
            and isinstance(receipt.decision, Operation)
            and receipt.decision.registered_wire == request.operation
            and receipt.decision.registered_turn == invocation.turn
            and receipt.decision.normalized_turn == invocation.turn
            for receipt in context.run.receipts
        )
    )


def _resume_ancestry(
    state: EvaluationState, context: EvaluationContext, invocation: Invocation, intent: Intent
) -> None:
    identity = (
        intent.request.continuation_id
        if isinstance(intent.request, ResumeSessionTurn)
        else invocation.turn.continuation_id
    )
    if identity is None:
        return
    previous = next((row for row in state.continuations if row.continuation_id == identity), None)
    if (
        previous is None
        or previous.phase not in (ContinuationPhase.AUTHORIZED, ContinuationPhase.RESUMED)
        or previous.next_invocation != invocation.invocation
        or invocation.turn.continuation_id not in (None, identity)
        or invocation.turn.predecessor not in (None, previous.invocation)
        or _invocation(context, previous).turn.session != invocation.turn.session
        or _invocation(context, previous).scope != invocation.scope
    ):
        raise ContractError(
            ("continuation", "invocation"),
            "resumed yield requires exact authorized continuation ancestry",
        )


def _yield_proof(
    state: EvaluationState,
    context: EvaluationContext,
    invocation: Invocation,
    continuation: Continuation,
    *,
    retained: bool = False,
) -> None:
    observation = invocation.observation
    if (
        invocation.phase not in (SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED)
        or invocation.turn.invocation_id != invocation.invocation.invocation_id
        or invocation.turn.session.session_id != invocation.invocation.session_id
        or invocation.scope.generation != invocation.invocation.generation
        or observation is None
        or not observation.accepted
        or not observation.terminal
        or observation.status != ObservationStatus.SUCCEEDED
        or observation.scope != invocation.scope
    ):
        raise ContractError(("continuation", "invocation"), "requires conclusive owned yield")
    session = next(
        (
            row
            for row in context.sessions.sessions
            if row.spec.session_id == invocation.invocation.session_id
        ),
        None,
    )
    intent = next(
        (row for row in context.intents.intents if row.request_id == observation.request_id), None
    )
    if (
        session is None
        or session.spec != invocation.turn.session
        or session.scope != invocation.scope
        or session.generation != invocation.invocation.generation
        or (
            not retained
            and (session.invocation != invocation.invocation.invocation_id or not session.accepted)
        )
        or session.phase
        not in (
            (SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED, SessionPhase.IDLE)
            if retained
            else (SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED)
        )
        or session.resource_id is None
        or intent is None
        or not _turn_matches(context, invocation, intent)
        or intent.phase != IntentPhase.COMPLETED
        or intent.observation != observation
        or observation.admission_id != intent.request.admission_id
    ):
        raise ContractError(
            ("continuation", "invocation"),
            "requires canonical accepted turn and session correspondence",
        )
    _resume_ancestry(state, context, invocation, intent)
    if intent.suspension is not None and any(
        getattr(intent.suspension, field) != getattr(continuation, field)
        for field in ("continuation_id", "invocation", "next_invocation", "jobs", "deadline_at")
    ):
        raise ContractError(
            ("continuation", "invocation"), "yielded manifest conflicts with canonical suspension"
        )
    if invocation.scope.owner == context.run.run_id:
        if invocation.turn.session.access == Access.WRITE_CANDIDATE:
            raise ContractError(
                ("continuation", "checkpoint"),
                "run-owned candidate writer has no retained checkpoint contract",
            )
        return
    attempt = _attempt(context, invocation.scope)
    if (
        attempt is None
        or intent.request.admission_id is None
        or (not retained and intent.request.admission_id != attempt.admission_id)
    ):
        raise ContractError(("continuation", "invocation"), "requires exact admitted turn episode")
    if not any(
        checkpoint.invocation == invocation.invocation for checkpoint in attempt.checkpoints
    ):
        raise ContractError(
            ("continuation", "checkpoint"), "requires a committed invocation checkpoint"
        )


class _PublicationVerdict:
    def __bool__(self) -> bool:
        message = "inspect the publication proof verdict explicitly"
        raise TypeError(message)


@dataclass(frozen=True, slots=True)
class _PublicationProven(_PublicationVerdict):
    cursor: EvaluationHistoryCursor


@dataclass(frozen=True, slots=True)
class _PublicationMissing(_PublicationVerdict):
    reason: Literal[
        "publication", "receipt", "predecessor", "source", "cursor", "history", "paid-prefix"
    ]


@dataclass(frozen=True, slots=True)
class _PublicationMismatch(_PublicationVerdict):
    field: Literal["publication", "source", "cursor"]


type _PublicationProof = _PublicationProven | _PublicationMissing | _PublicationMismatch


def _previous_publication(
    state: EvaluationState, context: EvaluationContext, invocation: Invocation
) -> _PublicationProof:
    """Project a unique exact previous publication, never the original paid prefix."""
    previous = tuple(
        row for row in state.continuations if row.next_invocation == invocation.invocation
    )
    if len(previous) != 1:
        return (
            _PublicationMismatch("publication") if previous else _PublicationMissing("publication")
        )
    continuation = previous[0]
    receipt = continuation.authorization_receipt
    predecessor = next(
        (row for row in context.sessions.invocations if row.invocation == continuation.invocation),
        None,
    )
    if receipt is None or predecessor is None:
        return _PublicationMissing("receipt" if receipt is None else "predecessor")
    sources = tuple(
        row
        for row in context.intents.intents
        if invocation.observation is not None
        and row.request_id == invocation.observation.request_id
    )
    if len(sources) != 1:
        return _PublicationMismatch("source") if sources else _PublicationMissing("source")
    source = sources[0]
    identity = (
        source.request.continuation_id
        if isinstance(source.request, ResumeSessionTurn)
        else invocation.turn.continuation_id
    )
    if identity != continuation.continuation_id:
        return _PublicationMismatch("publication")
    proof = _publication_identity(continuation, invocation, predecessor, receipt)
    if not isinstance(proof, _PublicationProven):
        return proof
    return _publication_history(context, invocation, predecessor, proof)


def _publication_identity(
    continuation: Continuation,
    invocation: Invocation,
    predecessor: Invocation,
    receipt: ResumeAuthorizationReceipt,
) -> _PublicationProof:
    if receipt.history_cursor is None:
        return _PublicationMissing("cursor")
    if (
        predecessor.scope != invocation.scope
        or predecessor.turn.session != invocation.turn.session
        or invocation.turn.predecessor not in (None, continuation.invocation)
        or receipt.continuation_id != continuation.continuation_id
        or receipt.next_invocation != invocation.invocation
        or receipt.timeout != continuation.timeout
        or receipt.evidence != continuation.evidence
    ):
        return _PublicationMismatch("publication")
    return _PublicationProven(receipt.history_cursor)


def _publication_history(
    context: EvaluationContext,
    invocation: Invocation,
    predecessor: Invocation,
    proof: _PublicationProven,
) -> _PublicationProof:
    owner = _attempt(context, invocation.scope)
    cursor = proof.cursor
    if (
        owner is None
        or owner.evaluation_history.availability != EvaluationHistoryAvailability.COMPLETE
    ):
        return _PublicationMissing("history")
    covered = owner.evaluation_history.covered_submissions
    prefix = predecessor.evaluation_prefix
    if prefix is None:
        return _PublicationMissing("paid-prefix")
    if (
        cursor.ordinal > len(covered)
        or (cursor.ordinal and covered[cursor.ordinal - 1] != cursor.submission_id)
        or (
            prefix.ordinal > cursor.ordinal
            or (prefix.ordinal and covered[prefix.ordinal - 1] != prefix.submission_id)
        )
    ):
        return _PublicationMismatch("cursor")
    return proof


def _suspend(
    state: EvaluationState, context: EvaluationContext, event: TurnSuspended
) -> AreaChange[EvaluationState]:
    continuation = event.continuation
    previous = next(
        (row for row in state.continuations if row.continuation_id == continuation.continuation_id),
        None,
    )
    if previous is not None:
        immutable = ("invocation", "next_invocation", "jobs", "deadline_at")
        if any(getattr(previous, name) != getattr(continuation, name) for name in immutable):
            raise ContractError(("continuation_id",), "immutable payload conflict")
        return AreaChange(state=state)
    invocation = _validate_new(state, context, continuation)
    if invocation.observation is None or invocation.observation.status == ObservationStatus.UNKNOWN:
        if not any(
            _turn_matches(context, invocation, intent) for intent in context.intents.intents
        ):
            raise ContractError(
                ("continuation", "invocation"), "inspection requires a canonical owned turn"
            )
        return AreaChange(
            state=state,
            requests=(
                InspectTurn(
                    request_id=RequestId(root=f"{continuation.continuation_id.root}/inspect-yield"),
                    scope=invocation.scope,
                    deadline_at=context.run.deadline_at,
                    invocation=invocation.invocation,
                ),
            ),
        )
    _yield_proof(state, context, invocation, continuation)
    publication = _previous_publication(state, context, invocation)
    continuation = continuation.model_copy(
        update={
            "preceding_submission": publication.cursor
            if isinstance(publication, _PublicationProven)
            else None
        }
    )
    history = tuple(
        row.model_copy(update={"phase": ContinuationPhase.RESUMED})
        if row.phase == ContinuationPhase.AUTHORIZED
        and row.next_invocation == continuation.invocation
        else row
        for row in state.continuations
    )
    updated = state.model_copy(update={"continuations": (*history, continuation)})
    _deadline_proof(updated, continuation)
    if _ready(updated, continuation):
        return _authorize(updated, context, continuation)
    requests: list[Request] = []
    for job in _jobs(state, continuation):
        if _settled(job):
            continue
        request_type = (
            InspectOwnedJob if job.status == ObservationStatus.UNKNOWN else ObserveOwnedJob
        )
        requests.append(
            request_type(
                scope=job.scope, resource_id=_resource(job), deadline_at=continuation.deadline_at
            )
        )
    return AreaChange(state=updated, requests=tuple(requests))


def _deadline(
    state: EvaluationState, context: EvaluationContext, continuation: Continuation, now_at: float
) -> AreaChange[EvaluationState]:
    if (
        now_at < continuation.deadline_at
        or continuation.phase not in (ContinuationPhase.WAITING, ContinuationPhase.PARKED)
        or continuation.timeout is not None
    ):
        return AreaChange(state=state)
    _job_ownership(state, context, continuation)
    _deadline_proof(state, continuation)
    if _ready(state, continuation):
        return AreaChange(state=state)
    unfinished = tuple(
        JobTimeout(resource_id=_resource(job), progress=job.progress)
        for job in _jobs(state, continuation)
        if not _settled(job)
    )
    frozen = continuation.model_copy(
        update={
            "timeout": TimedOut(
                deadline_at=continuation.deadline_at, reached_at=now_at, unfinished=unfinished
            ),
            "evidence": _feedback_evidence(
                tuple(
                    item
                    for job in _jobs(state, continuation)
                    if _settled(job)
                    for item in job.evidence
                )
            ),
        }
    )
    change = _authorize(_store(state, frozen), context, frozen)
    return change.model_copy(
        update={
            "signals": (
                *change.signals,
                *tuple(
                    JobTerminationRequested(resource_id=job.resource_id, cause="deadline")
                    for job in unfinished
                ),
            )
        }
    )


def _changed(
    state: EvaluationState, context: EvaluationContext, event: ContinuationJobsChanged
) -> AreaChange[EvaluationState]:
    events: list[StrategyEvent] = []
    signals: list[Signal] = []
    requests: list[Request] = []
    for continuation in state.continuations:
        if (
            event.resource_id not in continuation.jobs
            or continuation.phase != ContinuationPhase.WAITING
        ):
            continue
        job = next(
            job for job in _jobs(state, continuation) if job.resource_id == event.resource_id
        )
        if job.observation != event.observation:
            continue
        _job_ownership(state, context, continuation)
        _deadline_proof(state, continuation)
        if job.status == ObservationStatus.UNKNOWN:
            requests.append(
                InspectOwnedJob(
                    request_id=RequestId(
                        root=f"inspect-job:{len(continuation.continuation_id.root)}:{continuation.continuation_id.root}:{len(_resource(job).root)}:{_resource(job).root}:{event.observation_sequence}"
                    ),
                    scope=job.scope,
                    resource_id=_resource(job),
                    deadline_at=continuation.deadline_at,
                )
            )
        change = _authorize(state, context, continuation)
        state = change.state
        events.extend(change.events)
        signals.extend(change.signals)
    return AreaChange(
        state=state, requests=tuple(requests), events=tuple(events), signals=tuple(signals)
    )


def _close_matches(context: EvaluationContext, scope: Scope, request: CloseAttemptScope) -> bool:
    attempt = _attempt(context, scope)
    if attempt is None:
        return False
    closure = attempt.closure
    return (
        closure is not None
        and closure.disposition == "park"
        and closure.authority == request.request_id
        and attempt.admission_id is not None
        and closure.admission_id == attempt.admission_id
        and request.scope == scope
        and request.attempt.attempt_id == attempt.attempt_id
        and request.attempt.generation == attempt.generation
        and request.admission_id == closure.admission_id
    )


def _retire(
    state: EvaluationState,
    context: EvaluationContext,
    continuation: Continuation,
    event: ContinuationRetireRequested,
) -> AreaChange[EvaluationState]:
    if continuation.phase in (ContinuationPhase.CANCELLED, ContinuationPhase.RESUMED):
        return AreaChange(state=state)
    if (
        event.disposition == "park"
        and continuation.phase == ContinuationPhase.AUTHORIZED
        and continuation.authorization_receipt is None
    ):
        raise ContractError(
            ("continuation", "authorization"),
            "parking authorized feedback requires a durable authorization receipt",
        )
    if event.disposition == "park" and event.park_authority is None:
        raise ContractError(("park_authority",), "parking requires exact cleanup authority")
    if event.disposition == "park":
        scope = _invocation(context, continuation).scope
        authority = next(
            (row for row in context.intents.intents if row.request_id == event.park_authority), None
        )
        if (
            authority is None
            or not isinstance(authority.request, CloseAttemptScope)
            or not _close_matches(context, scope, authority.request)
        ):
            raise ContractError(("park_authority",), "requires owned canonical scope close")
    _job_ownership(state, context, continuation)
    phase = ContinuationPhase.PARKED if event.disposition == "park" else ContinuationPhase.CANCELLED
    if continuation.phase == phase and continuation.park_authority == event.park_authority:
        return AreaChange(state=state)
    retired = continuation.model_copy(
        update={
            "phase": phase,
            "park_authority": event.park_authority,
            "reopen_authority": None,
            "cancelled_resolutions": (),
        }
    )
    signals: tuple[Signal, ...] = tuple(
        JobTerminationRequested(resource_id=_resource(job), cause="retirement")
        for job in _jobs(state, continuation)
        if not _settled(job)
    )
    return AreaChange(state=_store(state, retired), signals=signals)


def _reopen_decision(
    context: EvaluationContext, request: ExecuteRegisteredOperation
) -> Operation | None:
    if not _declared_operation(
        context,
        request,
        LifecycleClass.IDEMPOTENT_WRITE,
        OperationNormalizationKind.SCOPE_REOPEN,
    ):
        return None
    for receipt in context.run.receipts:
        decision = receipt.decision
        if (
            not isinstance(receipt.feedback, Accepted)
            or not isinstance(decision, Operation)
            or decision.registered_scope_reopen is None
        ):
            continue
        identity = f"operation:{decision.decision_id.root}"
        if (
            receipt.decision_id == decision.decision_id
            and receipt.feedback.decision_id == receipt.decision_id
            and receipt.completion not in (CompletionStatus.FAILED, CompletionStatus.CANCELLED)
            and decision.registered_wire == request.operation
            and decision.normalized_scope_reopen == decision.registered_scope_reopen
            and decision.scope == request.scope
            and decision.deadline_at == request.deadline_at
            and request.scope == Scope(owner=context.run.run_id, generation=context.run.generation)
            and request.operation_id == OperationId(root=identity)
            and request.request_id == RequestId(root=identity)
            and request.retry_limit == context.run.limits.max_retries
            and request.decision_id in (None, decision.decision_id)
        ):
            return decision
    return None


def _reopen(
    state: EvaluationState,
    context: EvaluationContext,
    continuation: Continuation,
    event: ContinuationReopenRequested,
) -> AreaChange[EvaluationState]:
    normalization = event.normalization
    decision = _reopen_decision(context, event.request)
    if decision is None or decision.registered_scope_reopen != normalization:
        raise ContractError(("normalization",), "requires canonical registered reopening proof")
    _job_ownership(state, context, continuation)
    _deadline_proof(state, continuation)
    if (
        continuation.phase == ContinuationPhase.REOPENING
        and continuation.reopen_authority == event.request.request_id
    ):
        if (
            continuation.cancelled_resolutions != normalization.resolved_cancelled_jobs
            or continuation.park_authority != normalization.park_authority
        ):
            raise ContractError(("normalization",), "reopen identity payload conflict")
        return AreaChange(state=state)
    receipt = next(row for row in context.run.receipts if row.decision_id == decision.decision_id)
    if receipt.completion is not None:
        raise ContractError(
            ("normalization",), "completed reopen decision cannot authorize another dispatch"
        )
    _released_dependencies(state, context, continuation)
    scope = _invocation(context, continuation).scope
    attempt = _attempt(context, scope)
    if (
        continuation.phase != ContinuationPhase.PARKED
        or attempt is None
        or attempt.phase != AttemptPhase.PARKED
        or attempt.closure is None
        or attempt.closure.disposition != "park"
        or attempt.closure.authority != continuation.park_authority
        or context.run.status != RunStatus.RUNNING
        or normalization.attempt.attempt_id != scope.owner
        or normalization.attempt.generation != scope.generation
        or continuation.park_authority != normalization.park_authority
        or event.request.request_id is None
        or not _ready(state, continuation)
    ):
        raise ContractError(("normalization",), "requires current ready parked ownership")
    cancelled = {
        job.resource_id
        for job in _jobs(state, continuation)
        if job.status == ObservationStatus.CANCELLED
    }
    if (
        len(set(normalization.resolved_cancelled_jobs))
        != len(normalization.resolved_cancelled_jobs)
        or set(normalization.resolved_cancelled_jobs) != cancelled
    ):
        raise ContractError(
            ("resolved_cancelled_jobs",), "must resolve exactly cancelled dependencies"
        )
    authority = next(
        (row for row in context.intents.intents if row.request_id == continuation.park_authority),
        None,
    )
    if (
        authority is None
        or not isinstance(authority.request, CloseAttemptScope)
        or not _close_matches(context, scope, authority.request)
        or authority.phase != IntentPhase.COMPLETED
        or authority.observation is None
        or authority.observation.status != ObservationStatus.SUCCEEDED
        or authority.observation.scope != scope
        or authority.observation.request_id != authority.request_id
        or authority.observation.admission_id != authority.request.admission_id
        or not authority.observation.accepted
        or not authority.observation.terminal
        or not authority.observation.released
    ):
        raise ContractError(("park_authority",), "requires positive completed cleanup proof")
    reopening = continuation.model_copy(
        update={
            "phase": ContinuationPhase.REOPENING,
            "reopen_authority": event.request.request_id,
            "cancelled_resolutions": normalization.resolved_cancelled_jobs,
        }
    )
    return AreaChange(
        state=_store(state, reopening),
        signals=(ScopeReopenRequested(request=event.request, normalization=normalization),),
    )


def _reopened(
    state: EvaluationState,
    context: EvaluationContext,
    continuation: Continuation,
    event: ContinuationScopeReopened,
) -> AreaChange[EvaluationState]:
    if (
        continuation.phase != ContinuationPhase.REOPENING
        or continuation.park_authority != event.park_authority
    ):
        return AreaChange(state=state)
    scope = _invocation(context, continuation).scope
    attempt = _attempt(context, scope)
    observation = event.observation
    authority = next(
        (row for row in context.intents.intents if row.request_id == continuation.reopen_authority),
        None,
    )
    decision = (
        _reopen_decision(context, authority.request)
        if authority is not None and isinstance(authority.request, ExecuteRegisteredOperation)
        else None
    )
    if decision is None or authority is None:
        return AreaChange(state=state)
    normalization = decision.registered_scope_reopen
    if (
        normalization is None
        or normalization.continuation_id != continuation.continuation_id
        or normalization.park_authority != continuation.park_authority
        or normalization.resolved_cancelled_jobs != continuation.cancelled_resolutions
        or normalization.attempt.attempt_id != scope.owner
        or normalization.attempt.generation != scope.generation
        or attempt is None
        or (
            attempt.closure is not None and attempt.closure.authority != continuation.park_authority
        )
    ):
        return AreaChange(state=state)
    if (
        (
            observation.status == ObservationStatus.UNKNOWN
            or (
                isinstance(authority.outcome, ScopedAdmissionReopenOutcome)
                and authority.outcome.admission == "unknown"
            )
        )
        and observation.request_id == continuation.reopen_authority
        and observation.scope == authority.request.scope
        and authority.observation == observation
        and observation.admission_id == attempt.admission_id
        and (
            not isinstance(authority.outcome, ScopedAdmissionReopenOutcome)
            or authority.outcome.scope == scope
        )
    ):
        return AreaChange(
            state=state,
            requests=(
                InspectRequest(
                    request_id=RequestId(root=f"{observation.request_id.root}/inspect-reopen"),
                    scope=authority.request.scope,
                    deadline_at=context.run.deadline_at,
                    target=observation.request_id,
                ),
            ),
        )
    if (
        observation.request_id != continuation.reopen_authority
        or observation.scope != authority.request.scope
        or authority.phase != IntentPhase.COMPLETED
        or authority.observation != observation
        or not isinstance(authority.outcome, ScopedAdmissionReopenOutcome)
        or authority.outcome.scope != scope
        or authority.outcome.admission != "reopened"
        or observation.status != ObservationStatus.SUCCEEDED
        or not observation.accepted
        or not observation.terminal
        or attempt is None
        or observation.admission_id is None
        or observation.admission_id != attempt.admission_id
        or not _active(context, scope)
    ):
        return AreaChange(state=state)
    waiting = continuation.model_copy(update={"phase": ContinuationPhase.WAITING})
    return _authorize(_store(state, waiting), context, waiting, reopened=True)


def advance(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Update only continuations; emit proof-gated feedback and cross-area signals."""
    if isinstance(event, TurnSuspended):
        return _suspend(state, context, event)
    if isinstance(event, ContinuationJobsChanged):
        return _changed(state, context, event)
    if not isinstance(
        event,
        DeadlineReached
        | ContinuationRetireRequested
        | ContinuationReopenRequested
        | ContinuationScopeReopened,
    ):
        raise ContractError(("event", "kind"), "event is not owned by continuations")
    identity = (
        event.normalization.continuation_id
        if isinstance(event, ContinuationReopenRequested)
        else event.continuation_id
    )
    continuation = next(
        (row for row in state.continuations if row.continuation_id == identity), None
    )
    if continuation is None:
        if isinstance(event, ContinuationReopenRequested):
            raise ContractError(("continuation_id",), "requires an owned parked continuation")
        return AreaChange(state=state)
    if isinstance(event, DeadlineReached):
        change = _deadline(state, context, continuation, event.now_at)
    elif isinstance(event, ContinuationRetireRequested):
        change = _retire(state, context, continuation, event)
    elif isinstance(event, ContinuationReopenRequested):
        change = _reopen(state, context, continuation, event)
    elif isinstance(event, ContinuationScopeReopened):
        change = _reopened(state, context, continuation, event)
    else:
        raise ContractError(("event", "kind"), "event is not owned by continuations")
    return change
