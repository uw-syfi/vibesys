"""Pure wait-all authorization and frozen deadlines.

CONT-BOUND remains a cutover prerequisite: scientific repeated-failure and
no-new-evaluation policy must consume the durable history and publication bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from ._comparison import comparisons_to_publish, moment_of
from ._deadlines import bound_to_run, cleanup, limit_to, time_remains
from ._evaluation_history import attempt_settled, produce_history
from ._proofs import admitted_work_proceeds
from ._registry import ContractError
from ._session_scope import dispatching_request
from ._submission_receipts import same_conclusion
from ._wait_clock import Bound, wait_bounds
from .types.attempts import (
    AttemptEvaluationHistoryUpdated,
    AttemptPhase,
)
from .types.common import (
    AttemptId,
    AttemptRef,
    CompletionStatus,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    RequestId,
    Scope,
)
from .types.evaluation import (
    Continuation,
    ContinuationJobsChanged,
    ContinuationPhase,
    ContinuationRetireRequested,
    DeadlineReached,
    InspectOwnedJob,
    JobTerminationRequested,
    ObserveOwnedJob,
    OwnedJob,
    RegisteredOwnedJob,
    ResumeAuthorizationReceipt,
    ResumeAuthorized,
    ResumeRefusal,
    ResumeRefused,
    TurnSuspended,
)
from .types.evaluation_history import AttemptEvaluationHistory, EvaluationHistoryAvailability
from .types.job_observations import JobTimeout, TimedOut, WaitEnd
from .types.kernel import AreaChange, EvaluationContext
from .types.records import Outbound, RequestPhase, within
from .types.sessions import Access, DispatchTurn, InspectTurn, ResumeSessionTurn, SessionPhase
from .types.strategy import Accepted, Operation

if TYPE_CHECKING:
    from collections.abc import Callable

    from .types.attempts import AttemptsState, AttemptView
    from .types.common import EvidenceKey, InvocationRef, Observation, ResourceId
    from .types.evaluation import (
        EvaluationEvent,
        EvaluationState,
        EvidenceRef,
    )
    from .types.evaluation_history import EvaluationHistoryCursor
    from .types.kernel import RunState, SessionsContext, Signal, StrategyEvent
    from .types.measurement import Comparison
    from .types.records import Request, RequestRecord
    from .types.sessions import Invocation, SessionsState


class SuspensionRefusal(StrEnum):
    """Why a scope cannot take a new suspension now.

    Each reason is caused by what an agent does or when it does it, so a caller that
    forwards an agent's request to wait asks ``suspension_refusal`` first and tells the
    agent, instead of letting the suspension reach the commit that would reject it.
    """

    SCOPE_NOT_ACTIVE = "scope_not_active"
    """The run is not running, or the scope's owner is retired, closing or unadmitted."""
    OPEN_CONTINUATION = "open_continuation"
    """An earlier continuation in the scope is still waiting for its single resume."""
    NOT_RESUMABLE = "not_resumable"
    """The attempt is exhausted, so core would refuse the resume."""
    PREFIX_MISMATCH = "prefix_mismatch"
    """The turn has no paid-cycle history prefix that matches the attempt's history.

    A turn dispatched while an earlier measurement of its attempt was still in flight has
    none, and core authorizes a resume only from an exact prefix.
    """

    JOB_NOT_OWNED = "job_not_owned"
    """A job the suspension waits on is not one of the scope's owned jobs."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The run has no time left for the suspension. A later deadline is bounded, not refused."""

    @property
    def detail(self) -> str:
        """The contract-error text for this reason."""
        return _REFUSAL_DETAIL[self]


_REFUSAL_DETAIL = {
    SuspensionRefusal.SCOPE_NOT_ACTIVE: "requires current active ownership",
    SuspensionRefusal.OPEN_CONTINUATION: "already owns an unfinished continuation",
    SuspensionRefusal.NOT_RESUMABLE: "attempt is exhausted, so its resume cannot be authorized",
    SuspensionRefusal.PREFIX_MISMATCH: "attempt resume requires exact paid-cycle history prefix",
    SuspensionRefusal.JOB_NOT_OWNED: "dependency requires exactly one job owned by the scope",
    SuspensionRefusal.DEADLINE_EXCEEDED: "run has no time left",
}


def paid_prefix_refusal(
    history: AttemptEvaluationHistory, prefix: EvaluationHistoryCursor | None
) -> SuspensionRefusal | None:
    """Whether ``prefix`` is an exact prefix of the attempt's covered submissions.

    One rule for two callers: the suspension gate asks it of the turn that wants to
    wait, and resume authorization asks it of the turn that waited. Coverage only grows,
    so a prefix that holds at the wait still holds at the resume.
    """
    if (
        prefix is None
        or prefix.ordinal > len(history.covered_submissions)
        or (
            prefix.ordinal
            and history.covered_submissions[prefix.ordinal - 1] != prefix.submission_id
        )
    ):
        return SuspensionRefusal.PREFIX_MISMATCH
    return None


def resume_refusal(
    history: AttemptEvaluationHistory, prefix: EvaluationHistoryCursor | None
) -> ResumeRefusal | None:
    """Why an attempt's turn that waited from ``prefix`` cannot resume on ``history``, or None.

    The one rule for both ends of a resume: authorization asks it of the history it
    publishes, and the resume's dispatch asks it of that same history as the attempt still
    stores it (``published_history``), so core never authorizes a resume that its dispatch
    would refuse.
    """
    if history.availability != EvaluationHistoryAvailability.COMPLETE:
        return ResumeRefusal.HISTORY_UNAVAILABLE
    if paid_prefix_refusal(history, prefix) is not None:
        return ResumeRefusal.PREFIX_MISMATCH
    return None


def published_history(
    history: AttemptEvaluationHistory, cursor: EvaluationHistoryCursor | None
) -> AttemptEvaluationHistory:
    """``history`` as it stood when core published ``cursor`` with a resume's authorization.

    The scope may submit again between the authorization and the resume's dispatch, and that
    submission leaves the whole stored history incomplete until it ends. (Core's own runs in
    the scope are not the attempt's history, so they never do: ``_evaluation_history``.)
    Coverage and records only grow, so the prefix through ``cursor`` is what authorization
    proved. With nothing submitted since, that is the stored history itself; no cursor, or one
    the stored coverage does not hold, is an unavailable history.
    """
    if (
        cursor is None
        or cursor.ordinal > len(history.covered_submissions)
        or (
            cursor.ordinal
            and history.covered_submissions[cursor.ordinal - 1] != cursor.submission_id
        )
    ):
        return AttemptEvaluationHistory()
    if cursor.ordinal == len(history.covered_submissions):
        return history
    covered = history.covered_submissions[: cursor.ordinal]
    records = tuple(record for record in history.records if record.ordinal <= cursor.ordinal)
    complete = tuple(record.submission_id for record in records) == covered
    return AttemptEvaluationHistory(
        availability=EvaluationHistoryAvailability.COMPLETE
        if complete
        else EvaluationHistoryAvailability.UNAVAILABLE,
        covered_submissions=covered,
        records=records,
    )


def exhausted(attempts: AttemptsState, scope: Scope) -> bool:
    """Whether ``scope`` is an attempt that ended (a spent paid limit, for one).

    Core authorizes no resume for such an attempt, so a suspension in it can never end.
    An attempt's history completes only after its measurements end, so history
    completeness is a property of the resume, not of the suspension.
    """
    return isinstance(scope.owner, AttemptId) and any(
        row.attempt_id == scope.owner
        and row.generation == scope.generation
        and row.terminal_reason is not None
        for row in attempts.attempts
    )


def _open(context: EvaluationContext, row: Continuation) -> bool:
    """Whether the continuation still owes its single resume.

    A waiting continuation does. An authorized one does until its
    successor invocation was dispatched: a successor that ran has consumed the resume,
    even when it ended without yielding again (nothing else marks it resumed then).
    """
    if row.phase == ContinuationPhase.WAITING:
        return True
    return row.phase == ContinuationPhase.AUTHORIZED and not any(
        held.invocation == row.next_invocation for held in context.sessions.invocations
    )


def wait_refused_at_turn_end(
    sessions: SessionsState, context: SessionsContext, invocation: Invocation, wait: Continuation
) -> bool:
    """Whether the suspension gate refuses ``wait`` against the state a turn's end produces."""
    evaluation = EvaluationContext(
        run=context.run, attempts=context.attempts, sessions=sessions, records=context.records
    )
    return wait_refusal_in(evaluation, context.evaluation, invocation, wait) is not None


def suspension_refusal_in(
    context: EvaluationContext,
    state: EvaluationState,
    scope: Scope,
    *,
    holder: InvocationRef | None = None,
) -> SuspensionRefusal | None:
    """The reason ``scope`` cannot take a new suspension, or None when it can.

    A suspension is claimed from the moment a turn is observed with one (it is pending
    on its invocation) until it is recorded as a continuation, so a second turn of the
    scope is refused while the first holds the claim. ``holder`` is the invocation asking:
    its own claim is not a reason to refuse it.
    """
    return suspension_gate(
        active=lambda: _active(context, scope),
        claimed=lambda: _claimed(context, state, scope, holder),
    )


def _claimed(
    context: EvaluationContext, state: EvaluationState, scope: Scope, holder: InvocationRef | None
) -> bool:
    return any(
        row.pending_suspension is not None and row.scope == scope and row.invocation != holder
        for row in context.sessions.invocations
    ) or any(
        _open(context, row) and _invocation(context, row).scope == scope
        for row in state.continuations
    )


def suspension_gate(
    *,
    active: Callable[[], bool],
    claimed: Callable[[], bool],
    owned: Callable[[], bool] | None = None,
    remains: Callable[[], bool] | None = None,
) -> SuspensionRefusal | None:
    """The one ordering of suspension refusals, whichever state supplies the facts.

    The positional core and the native replay log each answer the same questions: is the
    scope active, does another wait already claim it, does it own every job it waits on,
    and is time left before the wait's capped bound. Each is asked only if the earlier
    ones pass. ``owned`` and ``remains`` are None when no continuation is checked yet.
    """
    if not active():
        return SuspensionRefusal.SCOPE_NOT_ACTIVE
    if claimed():
        return SuspensionRefusal.OPEN_CONTINUATION
    if owned is not None and not owned():
        return SuspensionRefusal.JOB_NOT_OWNED
    if remains is not None and not remains():
        return SuspensionRefusal.DEADLINE_EXCEEDED
    return None


def commit_refusal_in(
    context: EvaluationContext,
    state: EvaluationState,
    invocation: Invocation,
    wait: Continuation | None = None,
) -> SuspensionRefusal | None:
    """Why recording a suspension of ``invocation`` would be refused, or None.

    These are the refusals core's commit of a suspension raises for what the world did
    (a peer's claim, a retired scope, a job the scope does not own), as opposed to a
    malformed continuation. With ``wait`` it also checks the continuation's jobs and
    deadline.
    """
    scope, jobs = invocation.scope, (*state.jobs, *state.registered_jobs)

    def active() -> bool:
        return _active(context, scope)

    def claimed() -> bool:
        return _claimed(context, state, scope, invocation.invocation)

    if wait is None:
        return suspension_gate(active=active, claimed=claimed)
    checked: Continuation = wait

    # An empty or repeating job list is a malformed continuation, not a refusal: it halts.
    def owned() -> bool:
        for identity in checked.jobs:
            matching = tuple(row for row in jobs if row.resource_id == identity)
            if len(matching) != 1 or matching[0].scope != scope:
                return False
        return True

    def remains() -> bool:
        return time_remains(context.run, wait_deadline(state, context, checked).at)

    return suspension_gate(active=active, claimed=claimed, owned=owned, remains=remains)


def _inspection(context: EvaluationContext, request: Request) -> Outbound:
    """A reconciling inspection gets core's own bounded cleanup time."""
    return cleanup(context.run, request)


def _until_wait_ends(
    state: EvaluationState, context: EvaluationContext, continuation: Continuation, request: Request
) -> Outbound:
    """A poll of a job an agent waits on ends when that wait's bound passes."""
    deadline = wait_deadline(state, context, continuation)
    return within(request, limit_to(context.run, deadline.at))


def _live(state: EvaluationState, wait: Continuation) -> tuple[OwnedJob | RegisteredOwnedJob, ...]:
    """``wait``'s jobs it still waits on: neither settled nor ended by their queue budget."""
    spent = {job.resource_id for job in wait.spent}
    return tuple(
        job for job in _jobs(state, wait) if not _settled(job) and job.resource_id not in spent
    )


def wait_deadline(state: EvaluationState, context: EvaluationContext, wait: Continuation) -> Bound:
    """When ``wait`` next changes, and why: at the earliest bound of the jobs it waits on.

    Each job's limit counts from its own start, and its queue budget bounds how long it may
    wait to start (``_wait_clock``). A job's queue budget ends that job alone; any other
    bound ends the wait. The bound is always a time; past the run deadline the run's end is
    the bound.
    """
    bounds = wait_bounds(state, context.records, context.run, wait, _live(state, wait))
    return min(bounds, key=lambda bound: bound.at, default=Bound(wait.deadline_at, WaitEnd.LIMIT))


def bounded_wait(run: RunState, wait: Continuation) -> Continuation:
    """``wait`` with its deadline bounded to the run, the form every gate and row sees."""
    deadline = bound_to_run(run, wait.deadline_at)
    return (
        wait if deadline == wait.deadline_at else wait.model_copy(update={"deadline_at": deadline})
    )


def wait_refusal_in(
    context: EvaluationContext,
    state: EvaluationState,
    invocation: Invocation,
    wait: Continuation | None = None,
) -> SuspensionRefusal | None:
    """Why ``invocation`` cannot suspend now, or None when it can: the one suspension gate.

    Asked when an agent requests a wait and again when its turn is observed with the
    suspension, before anything durable claims it. It adds the refusals that are valid
    to commit but make the resume raise (an exhausted attempt, a prefix mismatch) to
    ``commit_refusal_in``.
    """
    refusal = commit_refusal_in(context, state, invocation, wait)
    if refusal is not None:
        return refusal
    if exhausted(context.attempts, invocation.scope):
        return SuspensionRefusal.NOT_RESUMABLE
    owner = next(
        (
            row
            for row in context.attempts.attempts
            if row.attempt_id == invocation.scope.owner
            and row.generation == invocation.scope.generation
        ),
        None,
    )
    if owner is not None:
        return paid_prefix_refusal(owner.evaluation_history, invocation.evaluation_prefix)
    return None


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
    if not admitted_work_proceeds(context.run):
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


def _descendant_released(state: EvaluationState, resource: ResourceId, scope: Scope) -> bool:
    jobs = tuple(
        job for job in (*state.jobs, *state.registered_jobs) if job.resource_id == resource
    )
    return len(jobs) == 1 and all(job.scope == scope and _job_released(job) for job in jobs)


def _descendants(
    state: EvaluationState, jobs: tuple[OwnedJob | RegisteredOwnedJob, ...], scope: Scope
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
        resources.update(descendants)
        if previous == (len(resources), len(sources)):
            break
    return descendants - {_resource(job) for job in jobs}


def _timeout_proof(continuation: Continuation) -> None:
    """A frozen timeout names only the wait's own jobs, and never precedes the stated deadline.

    Only a job's queue budget may end a wait before the deadline its agent stated.
    """
    timeout = continuation.timeout
    if timeout is not None and (
        (timeout.by != WaitEnd.QUEUE_BUDGET and timeout.deadline_at < continuation.deadline_at)
        or any(item.resource_id not in continuation.jobs for item in timeout.unfinished)
        or any(item not in timeout.unfinished for item in continuation.spent)
    ):
        raise ContractError(
            ("continuation", "timeout"),
            "frozen timeout must not precede its stated deadline and names owned dependencies",
        )


def _ready(state: EvaluationState, continuation: Continuation) -> bool:
    """Whether the wait has ended: it froze, or every job settled and none ran out of budget.

    A wait one of whose jobs spent its queue budget ends by freezing (``_deadline``), so the
    agent hears which job ran out.
    """
    return continuation.timeout is not None or (
        not continuation.spent and all(_settled(job) for job in _jobs(state, continuation))
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
) -> AreaChange[EvaluationState]:
    _job_ownership(state, context, continuation)
    if continuation.phase != ContinuationPhase.WAITING or not _ready(state, continuation):
        return AreaChange(state=state)
    _timeout_proof(continuation)
    invocation = _invocation(context, continuation)
    if not _active(context, invocation.scope):
        return AreaChange(state=state)
    if not attempt_settled(invocation.scope, state, context.records):
        # Stay WAITING: each submission's conclusive observation wakes this again.
        return AreaChange(state=state)
    _successor(context, continuation)
    _yield_proof(state, context, invocation, continuation)
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
    return _publish(state, context, continuation, invocation, evidence)


def _publish(
    state: EvaluationState,
    context: EvaluationContext,
    continuation: Continuation,
    invocation: Invocation,
    evidence: tuple[EvidenceRef, ...],
) -> AreaChange[EvaluationState]:
    """Authorize the settled wait's resume once, or refuse it for good (``resume_refusal``)."""
    owner = _attempt(context, invocation.scope)
    history = (
        produce_history(invocation.scope, state, context.records, owner, context.run)
        if owner
        else None
    )
    if history is not None:
        # Every submission has ended (``attempt_settled``), so this history is final: a
        # refusal now is a refusal forever, and the wait ends without a resume.
        refusal = resume_refusal(history, invocation.evaluation_prefix)
        if refusal is not None:
            return _refuse(state, continuation, refusal, _history_update(owner, history))
    cursor = history.cursor if history is not None else None
    comparisons, settling = comparisons_to_publish(
        state,
        invocation.scope,
        (
            job.plan.measured_input
            for job in _jobs(state, continuation)
            if isinstance(job, OwnedJob) and job.plan.measured_input is not None
        ),
        _told(state, context, invocation.scope),
        moment_of(context.run),
    )
    publication = ResumeAuthorizationReceipt(
        continuation_id=continuation.continuation_id,
        next_invocation=continuation.next_invocation,
        evidence=evidence,
        timeout=continuation.timeout,
        history_cursor=cursor,
        comparisons=comparisons,
        settling=settling,
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
                comparisons=comparisons,
                settling=settling,
            ),
        ),
        signals=_history_update(owner, history),
    )


def _told(
    state: EvaluationState, context: EvaluationContext, scope: Scope
) -> tuple[tuple[Comparison, bool], ...]:
    """Every comparison the scope's earlier resumes told of, oldest first, and if it was settling.

    Read from each authorized continuation's receipt (``ResumeAuthorizationReceipt.settling``).
    """
    scopes = {row.invocation: row.scope for row in context.sessions.invocations}
    return tuple(
        (comparison, comparison.candidate_input in receipt.settling)
        for row in state.continuations
        if (receipt := row.authorization_receipt) is not None
        and scopes.get(row.invocation) == scope
        for comparison in receipt.comparisons
    )


def _history_update(
    owner: AttemptView | None, history: AttemptEvaluationHistory | None
) -> tuple[Signal, ...]:
    """The signal storing the history a continuation's end published, when it has one.

    The ends of the jobs a continuation awaits leave publishing the history to it.
    """
    if owner is None or history is None:
        return ()
    return (
        AttemptEvaluationHistoryUpdated(
            attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
            history=history,
        ),
    )


def _refuse(
    state: EvaluationState,
    continuation: Continuation,
    reason: ResumeRefusal,
    signals: tuple[Signal, ...],
) -> AreaChange[EvaluationState]:
    """End a wait whose resume core will never authorize: cancel it and say why."""
    cancelled = continuation.model_copy(update={"phase": ContinuationPhase.CANCELLED})
    return AreaChange(
        state=_store(state, cancelled),
        events=(
            ResumeRefused(
                continuation_id=continuation.continuation_id,
                invocation=continuation.invocation,
                reason=reason,
            ),
        ),
        signals=signals,
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
        or continuation.spent
        or continuation.evidence
        or continuation.authorization_receipt is not None
        or continuation.preceding_submission is not None
    ):
        raise ContractError(("continuation",), "new suspension must contain only waiting record")
    if not continuation.jobs or len(set(continuation.jobs)) != len(continuation.jobs):
        raise ContractError(("continuation", "jobs"), "requires distinct nonempty dependencies")
    _successor(context, continuation)
    current = continuation.invocation
    invocation = _invocation(context, continuation)
    if (
        invocation.turn.invocation_id != current.invocation_id
        or invocation.turn.session.session_id != current.session_id
        or invocation.scope.generation != current.generation
    ):
        raise ContractError(("continuation", "invocation"), "requires current active ownership")
    if any(job.scope != invocation.scope for job in _jobs(state, continuation)):
        raise ContractError(("continuation", "jobs"), "dependency belongs to a different scope")
    if refusal := commit_refusal_in(context, state, invocation, continuation):
        raise ContractError(("continuation", "invocation"), refusal.detail)
    if any(
        row.invocation == current or row.next_invocation == continuation.next_invocation
        for row in state.continuations
    ):
        raise ContractError(
            ("continuation", "invocation"), SuspensionRefusal.OPEN_CONTINUATION.detail
        )
    return invocation


def _declared_operation(
    context: EvaluationContext,
    request: ExecuteRegisteredOperation,
    lifecycle: LifecycleClass,
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
    )


def _turn_matches(
    context: EvaluationContext, invocation: Invocation, record: RequestRecord
) -> bool:
    request = record.request
    if record.lifecycle != LifecycleClass.SESSION_TURN or request.request_id != record.request_id:
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
    state: EvaluationState,
    context: EvaluationContext,
    invocation: Invocation,
    record: RequestRecord,
) -> None:
    identity = (
        record.request.continuation_id
        if isinstance(record.request, ResumeSessionTurn)
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
    record = context.records.record(observation.request_id)
    if (
        session is None
        or session.spec != invocation.turn.session
        or session.scope != invocation.scope
        or session.generation != invocation.invocation.generation
        or session.invocation != invocation.invocation.invocation_id
        or not session.accepted
        or session.phase not in (SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED)
        or session.resource_id is None
        or record is None
        or not _turn_matches(context, invocation, record)
        or record.phase != RequestPhase.COMPLETED
        or record.observation != observation
        or observation.admission_id != record.request.admission_id
    ):
        raise ContractError(
            ("continuation", "invocation"),
            "requires canonical accepted turn and session correspondence",
        )
    _resume_ancestry(state, context, invocation, record)
    if record.suspension is not None and any(
        getattr(record.suspension, field) != getattr(continuation, field)
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
        or record.request.admission_id is None
        or record.request.admission_id != attempt.admission_id
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
        for row in context.records.records
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
    # The history as published: the scope may have submitted again since (``published_history``).
    if (
        owner is None
        or published_history(owner.evaluation_history, cursor).availability
        != EvaluationHistoryAvailability.COMPLETE
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
    continuation = bounded_wait(context.run, event.continuation)
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
            _turn_matches(context, invocation, record) for record in context.records.records
        ):
            raise ContractError(
                ("continuation", "invocation"), "inspection requires a canonical owned turn"
            )
        return AreaChange(
            state=state,
            requests=(
                _inspection(
                    context,
                    InspectTurn(
                        request_id=RequestId(
                            root=f"{continuation.continuation_id.root}/inspect-yield"
                        ),
                        scope=invocation.scope,
                        invocation=invocation.invocation,
                        dispatch=dispatching_request(context.records, invocation),
                    ),
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
    _timeout_proof(continuation)
    # The strategy learns of the suspension exactly once, when the continuation is
    # first recorded; a replay returns above without events.
    notice: tuple[StrategyEvent, ...] = (TurnSuspended(continuation=continuation),)
    if _ready(updated, continuation):
        change = _authorize(updated, context, continuation)
        return change.model_copy(update={"events": (*notice, *change.events)})
    requests: list[Outbound] = []
    for job in _jobs(state, continuation):
        if _settled(job):
            continue
        request_type = (
            InspectOwnedJob if job.status == ObservationStatus.UNKNOWN else ObserveOwnedJob
        )
        requests.append(
            _until_wait_ends(
                state,
                context,
                continuation,
                request_type(scope=job.scope, resource_id=_resource(job)),
            )
        )
    return AreaChange(state=updated, requests=tuple(requests), events=notice)


def _deadline(
    state: EvaluationState, context: EvaluationContext, continuation: Continuation, now_at: float
) -> AreaChange[EvaluationState]:
    """Apply every bound of ``continuation`` the clock has reached at ``now_at``.

    A job whose queue budget ran out ends alone: it joins ``spent`` and core cancels it, and
    the wait goes on with the jobs it still waits on. Any other bound of a job it waits on
    ends the whole wait, freezing every unfinished job. A wait left with no job to wait on
    after one spent its budget ends at once, by that budget.
    """
    if continuation.phase != ContinuationPhase.WAITING or continuation.timeout is not None:
        return AreaChange(state=state)
    _job_ownership(state, context, continuation)
    live = _live(state, continuation)
    bounds = dict(
        zip(
            (_resource(job) for job in live),
            wait_bounds(state, context.records, context.run, continuation, live),
            strict=True,
        )
    )
    ends = tuple(
        bound
        for bound in bounds.values()
        if bound.by != WaitEnd.QUEUE_BUDGET and bound.at <= now_at
    )
    ending = min(ends, key=lambda bound: bound.at, default=None)
    # A budget that ran out before the wait ended ends its job alone; one the wait's end
    # came first for is that end's.
    spends_until = now_at if ending is None else ending.at
    spending = tuple(
        job
        for job in live
        if bounds[_resource(job)].by == WaitEnd.QUEUE_BUDGET
        and bounds[_resource(job)].at <= spends_until
    )
    spent = (
        *continuation.spent,
        *(
            JobTimeout(
                resource_id=_resource(job),
                progress=job.progress,
                by=WaitEnd.QUEUE_BUDGET,
                at=bounds[_resource(job)].at,
            )
            for job in spending
        ),
    )
    cancels = tuple(
        JobTerminationRequested(resource_id=_resource(job), cause="deadline") for job in spending
    )
    updated = continuation.model_copy(update={"spent": spent})
    still = tuple(job for job in live if job not in spending)
    if ending is not None:
        deadline = ending
    elif spent and not still:
        deadline = Bound(at=max(item.at for item in spent), by=WaitEnd.QUEUE_BUDGET)
    elif spending:
        return AreaChange(state=_store(state, updated), signals=cancels)
    else:
        return AreaChange(state=state)
    by_resource = {item.resource_id: item for item in spent}
    unfinished = tuple(
        by_resource.get(
            _resource(job),
            JobTimeout(
                resource_id=_resource(job), progress=job.progress, by=deadline.by, at=deadline.at
            ),
        )
        for job in _jobs(state, continuation)
        if _resource(job) in by_resource or job in still
    )
    frozen = updated.model_copy(
        update={
            "timeout": TimedOut(
                deadline_at=deadline.at, reached_at=now_at, unfinished=unfinished, by=deadline.by
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
                *cancels,
                *tuple(
                    JobTerminationRequested(resource_id=_resource(job), cause="deadline")
                    for job in still
                ),
            )
        }
    )


def reach_deadlines(
    state: EvaluationState, context: EvaluationContext
) -> AreaChange[EvaluationState]:
    """Freeze every wait whose deadline the run clock has reached, as ``DeadlineReached`` does.

    Run time moves only with the events core consumes, and the event that carries it past
    a wait's deadline is often a fact about the very job the wait is on: the next poll of a
    job still queued. The timeout must record the jobs as they were at the deadline, so the
    evaluation area runs this before it applies any event, while the jobs still hold the
    facts they had then. A job's new fact can also move its bound earlier (a job that was
    pending starts, so the credit its pending fact could still earn is gone), so the area
    runs this again after the event: a wait whose bound already passed freezes at once.
    """
    change: AreaChange[EvaluationState] = AreaChange(state=state)
    for identity in tuple(row.continuation_id for row in state.continuations):
        row = next(r for r in change.state.continuations if r.continuation_id == identity)
        if row.timeout is not None:
            continue
        reached = _deadline(change.state, context, row, context.run.now_at)
        change = AreaChange(
            state=reached.state,
            signals=(*change.signals, *reached.signals),
            requests=(*change.requests, *reached.requests),
            events=(*change.events, *reached.events),
        )
    return change


def _changed(
    state: EvaluationState, context: EvaluationContext, event: ContinuationJobsChanged
) -> AreaChange[EvaluationState]:
    events: list[StrategyEvent] = []
    signals: list[Signal] = []
    requests: list[Outbound] = []
    for continuation in state.continuations:
        if (
            event.resource_id not in continuation.jobs
            or continuation.phase != ContinuationPhase.WAITING
        ):
            continue
        job = next(
            job for job in _jobs(state, continuation) if job.resource_id == event.resource_id
        )
        # A wake may carry an earlier reading of the job's end than the job holds now.
        if not same_conclusion(job.observation, event.observation):
            continue
        _job_ownership(state, context, continuation)
        _timeout_proof(continuation)
        if job.status == ObservationStatus.UNKNOWN:
            requests.append(
                _until_wait_ends(
                    state,
                    context,
                    continuation,
                    InspectOwnedJob(
                        request_id=RequestId(
                            root=f"inspect-job:{len(continuation.continuation_id.root)}:{continuation.continuation_id.root}:{len(_resource(job).root)}:{_resource(job).root}:{event.observation_sequence}"
                        ),
                        scope=job.scope,
                        resource_id=_resource(job),
                    ),
                )
            )
        change = _authorize(state, context, continuation)
        state = change.state
        events.extend(change.events)
        signals.extend(change.signals)
    return AreaChange(
        state=state, requests=tuple(requests), events=tuple(events), signals=tuple(signals)
    )


def _retire(
    state: EvaluationState,
    context: EvaluationContext,
    continuation: Continuation,
) -> AreaChange[EvaluationState]:
    if continuation.phase in (ContinuationPhase.CANCELLED, ContinuationPhase.RESUMED):
        return AreaChange(state=state)
    _job_ownership(state, context, continuation)
    retired = continuation.model_copy(update={"phase": ContinuationPhase.CANCELLED})
    signals: tuple[Signal, ...] = tuple(
        JobTerminationRequested(resource_id=_resource(job), cause="retirement")
        for job in _jobs(state, continuation)
        if not _settled(job)
    )
    # A cancel ends a wait whose resume core has not authorized: the suspended turn hears why,
    # as it hears any wait core will never resume (S191), instead of waiting out the run.
    events: tuple[ResumeRefused, ...] = ()
    if continuation.phase != ContinuationPhase.AUTHORIZED:
        events = (
            ResumeRefused(
                continuation_id=continuation.continuation_id,
                invocation=continuation.invocation,
                reason=ResumeRefusal.ATTEMPT_CLOSED,
            ),
        )
    return AreaChange(state=_store(state, retired), signals=signals, events=events)


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
        DeadlineReached | ContinuationRetireRequested,
    ):
        raise ContractError(("event", "kind"), "event is not owned by continuations")
    continuation = next(
        (row for row in state.continuations if row.continuation_id == event.continuation_id),
        None,
    )
    if continuation is None:
        return AreaChange(state=state)
    if isinstance(event, DeadlineReached):
        change = _deadline(state, context, continuation, event.now_at)
    elif isinstance(event, ContinuationRetireRequested):
        change = _retire(state, context, continuation)
    else:
        raise ContractError(("event", "kind"), "event is not owned by continuations")
    return change
