"""Session lifecycle executors: CancelTurn, CloseSession and ResumeSessionTurn.

They sit beside ``RuntimeSessionRequests`` (EnsureSession, DispatchTurn, InspectTurn)
and read its durable tables: the ``SessionBinding`` of a session (spec, conversation
key, resource) and the ``DispatchRecord`` of an invocation. Each request runs at most
once per canonical identity through the shared ``ReceiptStore`` (begun marker, fence
and lease check, sealed result) and reports through the shared ``ObservationFactory``.

Mechanisms:

* **CancelTurn** first closes the invocation's effect fence (``EffectFences``), which
  every dispatch enters before its provider call: no dispatch can begin afterwards. A
  fence no dispatch entered proves nothing is held. Otherwise it never assumes a stop.
  It asks ``AgentSessions.cancel`` to stop the turn this instance runs, then reads the
  invocation's journal row. No row: a dispatch still inside the fence on this host may
  journal and call yet (Unknown), and one that left or whose host was replaced never
  will. A ``Pending`` row means the dispatch call has not returned, here or in an
  earlier host that a restart replaced: Unknown, because a new host cannot prove the
  old provider process stopped. A ``Completed`` or ``InvalidResponse`` row shows the
  turn ended. An ``Unknown`` row was written by a dispatch call that returned, so the
  key is released with ``release_interrupted``, which itself refuses an active
  invocation.
* **CloseSession** first closes the episode's fence, which every EnsureSession enters
  in the same locked step as it binds, then releases the key's live provider resources
  with ``AgentSessions.release`` and keeps the binding and the provider checkpoint, so
  a reused conversation is reattached and resumed later. An active invocation makes it
  Unknown: the shell cancels first. No binding proves nothing is held.
* **ResumeSessionTurn** continues the retained conversation and never substitutes a
  fresh one. The first resume of a continuation binds it to one successor invocation
  and to the retained provider conversation; another successor, or another
  conversation under the same continuation, is REJECTED. The dispatch is a
  ``DispatchTurn`` of the same turn whose receipt is keyed by a derived identity (its
  dispatch record names this resume request, the one core knows), so it gets the
  write-ahead journal, replay of a recorded outcome and Unknown-never-repeated of
  ``RuntimeSessionRequests``; its facts are re-observed under the resume request.

The executor never returns ``ExecutorRefusal``: every inability is a typed
observation. Only terminal observations are stored.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Protocol, assert_never

from pydantic import BaseModel, ConfigDict

from vs_agent.api import (
    AgentSessionKey,
    Completed,
    InvalidResponse,
    InvocationConflictError,
    Pending,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
    Unknown,
)
from vs_core.api import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    InvocationRef,
    ObservationStatus,
    RequestId,
    RequestObserved,
    ResumeSessionTurn,
    SessionObserved,
    TurnObserved,
)
from vs_runtime._access_settlement import (
    AccessKey,
    AccessSettlement,
    AccessSettlementError,
    access_unproven,
)
from vs_runtime._core_identity import CoreRequestId, core_identity
from vs_runtime._core_requests import ExecutionContext, ExecutionOutcome, ExecutionResult
from vs_runtime._effect_fence import EffectFences, MayHold, NothingHeld, Unrecorded
from vs_runtime._observation_factory import (
    ObservationFactory,
    ObservationFacts,
    ObservationSubject,
)
from vs_runtime._receipt_store import (
    Conflict,
    Declined,
    Performed,
    ReceiptCorruptError,
    Replayed,
    Settled,
    Transient,
    owner_key,
)
from vs_runtime._session_requests import (
    SessionBinding,
    conversation_established,
    episode_fence,
    invocation_fence,
    load_session_binding,
    session_binding_key,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_agent.api import AgentInvocationRecord, AgentSessionCheckpoint, AgentSessions
    from vs_core.api import RequestBase, SessionId, SnapshotAndRetainRun
    from vs_runtime._core_requests import OwnerEvent, SessionRoleRequest
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._workspace_requests import RunInvocationProof
    from vs_sim.api import BlockingRunner

_CONTINUATIONS = "session-continuations"
_HANDLED = (CancelTurn, CloseSession, ResumeSessionTurn)
_SESSION_ERRORS = (InvocationConflictError, SessionConfigurationError, SessionPersistenceError)


class TurnDispatcher(Protocol):
    """Dispatches one turn exactly as ``DispatchTurn`` does (``RuntimeSessionRequests``)."""

    async def execute(
        self, request: SessionRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome:
        """Execute, replay or inspect the dispatch under its canonical identity."""
        ...

    async def dispatch_on_behalf_of(
        self, request: DispatchTurn, behalf: CoreRequestId, context: ExecutionContext
    ) -> ExecutionOutcome:
        """Dispatch the turn of a request core sent as another kind, recording *behalf*."""
        ...

    async def attempt(
        self,
        request: DispatchTurn,
        context: ExecutionContext,
        *,
        mark: str,
        behalf: CoreRequestId | None = None,
    ) -> ExecutionResult:
        """Run one attempt of the turn, recording no receipt of its result (see the runner)."""
        ...


type ResumedDispatch = Callable[
    [DispatchTurn, CoreRequestId, ExecutionContext], Awaitable[ExecutionOutcome]
]
"""How the turn of a resume is dispatched: with a receipt of its result, or as one attempt."""


class ContinuationBinding(BaseModel):
    """What the first resume of one continuation fixed: its successor and conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    invocation_id: str
    provider_session_id: str


def _rejected(diagnostic: str, binding: SessionBinding | None = None) -> ObservationFacts:
    return ObservationFacts(
        status=ObservationStatus.REJECTED,
        diagnostic=diagnostic,
        resource_id=None if binding is None else binding.resource_id,
    )


def _refused_before_dispatch(facts: ObservationFacts) -> ObservationFacts:
    """Facts of a resume that ended before its dispatch: a terminal refusal holds nothing.

    The refusal proves only that this turn holds nothing. The session it names stays held,
    so the answer drops the session's resource rather than report it released.
    """
    if facts.terminal and facts.status is ObservationStatus.REJECTED:
        return ObservationFacts.refused(facts.diagnostic)
    return facts


def _unknown(diagnostic: str, binding: SessionBinding | None = None) -> ObservationFacts:
    return ObservationFacts(
        status=ObservationStatus.UNKNOWN,
        terminal=False,
        diagnostic=diagnostic,
        resource_id=None if binding is None else binding.resource_id,
    )


def _released(
    status: ObservationStatus, binding: SessionBinding, diagnostic: str = ""
) -> ObservationFacts:
    return ObservationFacts(
        status=status,
        accepted=True,
        released=True,
        children_complete=True,
        resource_id=binding.resource_id,
        diagnostic=diagnostic,
    )


def _journal_row(
    sessions: AgentSessions, key: AgentSessionKey, invocation_id: str
) -> AgentInvocationRecord | None:
    """The raw journal row: ``inspect`` hides whether an unfinished dispatch is recovered."""
    with sessions.invocation_transaction() as slot:
        state = slot.load_optional()
    record = None if state is None else state.invocations.get(invocation_id)
    if record is not None and record.outcome.session_key != str(key):
        detail = f"invocation {invocation_id} belongs to another key"
        raise InvocationConflictError.because(detail)
    return record


def _writer_ended(row: AgentInvocationRecord | None) -> bool:
    """Whether the journal row proves the invocation's writer ended: judged or released."""
    if row is None:
        return False
    if isinstance(row.outcome, (Completed, InvalidResponse)):
        return True
    return row.interrupted and isinstance(row.outcome, Unknown)


def _call_returned(row: AgentInvocationRecord | None) -> bool:
    """Whether the dispatch call of the row returned, so releasing the key ended its turn.

    An Unknown row was written by a call that returned; a Pending row (a call in
    flight, or one a restart cannot account for) never qualifies.
    """
    return _writer_ended(row) or (row is not None and isinstance(row.outcome, Unknown))


class ReleasedRunInvocations:
    """Run-invocation proof that also accepts a turn CancelTurn interrupted and released.

    ``JournalRunInvocations`` proves settled turns. A returned dispatch that left an
    Unknown row and was then released by ``release_interrupted`` has no live writer
    either; a recovered Pending row never qualifies, because it is never released.
    Either way the turn's access receipt must be settled: ended but unjudged is not
    proof.
    """

    def __init__(
        self, settled: RunInvocationProof, sessions: AgentSessions, store: ReceiptStore
    ) -> None:
        """Wrap the settled-turn proof; read the same journal and bindings."""
        self._settled = settled
        self._sessions = sessions
        self._store = store

    def unproven(self, request: SnapshotAndRetainRun) -> str | None:
        """None when the turn settled or was interrupted and released; else why not."""
        reason = self._settled.unproven(request)
        if reason is None:
            return None
        invocation = request.invocation
        try:
            bkey = session_binding_key(request, invocation.session_id)
            binding = load_session_binding(self._store, bkey)
            if binding is None:
                return reason
            unsettled = access_unproven(
                self._store, AccessKey(binding=bkey, invocation=invocation.invocation_id.root)
            )
            if unsettled is not None:
                return unsettled
            row = _journal_row(
                self._sessions,
                AgentSessionKey.parse(binding.session_key),
                invocation.invocation_id.root,
            )
        except (ReceiptCorruptError, *_SESSION_ERRORS) as error:
            return f"invocation evidence is unreadable: {error}"
        if _writer_ended(row):
            return None
        return reason


class SessionLifecycleRequests:
    """Execute CancelTurn, CloseSession and ResumeSessionTurn once each."""

    def __init__(
        self,
        sessions: AgentSessions,
        dispatcher: TurnDispatcher,
        store: ReceiptStore,
        settlement: AccessSettlement,
        blocking: BlockingRunner,
    ) -> None:
        """Bind the durable agent sessions, the turn dispatcher, the store and its settlement.

        *settlement* is the instance the turn dispatcher records receipts with; *blocking*
        runs the journal reads and provider calls off the event loop.
        """
        self._sessions = sessions
        self._dispatcher = dispatcher
        self._store = store
        self._settlement = settlement
        self._blocking = blocking
        self._fences = EffectFences(store)
        self._observations = ObservationFactory(store)

    async def execute(
        self, request: SessionRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome:
        """Execute, replay or inspect one request under its canonical identity."""
        request_id = request.request_id
        if request_id is None:
            message = "request_id: execution requires a canonical identity"
            raise ValueError(message)
        if not isinstance(request, _HANDLED):
            return self._result(request, context, _rejected(f"{request.kind} is not executed here"))

        async def perform(
            *, resumed: bool
        ) -> Settled[ExecutionResult] | Transient[ExecutionResult]:
            del resumed  # every step inspects durable evidence before it acts
            try:
                result = await self._perform(request, request_id, context)
            except ReceiptCorruptError as error:
                result = self._result(request, context, _unknown(str(error)))
            return Settled(result) if result.observation.observation.terminal else Transient(result)

        execution = await self._store.run_once(
            request_id.root,
            owner=owner_key(request),
            context=context,
            result_type=ExecutionResult,
            perform=perform,
        )
        match execution:
            case Replayed(result) | Performed(result):
                return result
            case Conflict():
                return self._result(request, context, ObservationFacts.payload_conflict())
            case Declined(reason):
                return self._result(request, context, _unknown(reason))
            case _:
                assert_never(execution)

    def _result(
        self,
        request: RequestBase,
        context: ExecutionContext,
        facts: ObservationFacts,
        *,
        turn: TurnObserved | None = None,
    ) -> ExecutionResult:
        observation = self._observations.observe(
            ObservationSubject.of(request), facts, observed_at=context.now_at
        )
        events: tuple[OwnerEvent, ...] = ()
        if isinstance(request, CloseSession):
            events = (SessionObserved(session_id=request.session_id, observation=observation),)
        elif isinstance(request, ResumeSessionTurn):
            invocation = InvocationRef(
                session_id=request.turn.session.session_id,
                invocation_id=request.turn.invocation_id,
                generation=request.scope.generation,
            )
            events = (
                TurnObserved(
                    invocation=invocation,
                    observation=observation,
                    output_schema=None if turn is None else turn.output_schema,
                    output_json=None if turn is None else turn.output_json,
                    suspension=None if turn is None else turn.suspension,
                ),
            )
        return ExecutionResult(
            observation=RequestObserved(
                observation=observation,
            ),
            owner_events=events,
        )

    async def _perform(
        self, request: SessionRoleRequest, request_id: RequestId, context: ExecutionContext
    ) -> ExecutionResult:
        match request:
            case CancelTurn():
                facts = await self._blocking.run(self._cancel, request, context)
                facts = await self._settled(
                    facts,
                    request,
                    request.invocation.session_id,
                    request.invocation.invocation_id.root,
                )
            case CloseSession():
                facts = await self._blocking.run(self._close, request, context)
                facts = await self._settled(facts, request, request.session_id, None)
            case ResumeSessionTurn():
                return await self._resume(request, request_id, context, self._dispatch_resumed)
            case _:
                facts = _rejected(f"{request.kind} is not executed here")
        return self._result(request, context, facts)

    def _binding(self, request: RequestBase, session: SessionId) -> SessionBinding | None:
        return load_session_binding(self._store, session_binding_key(request, session))

    async def _settled(
        self,
        facts: ObservationFacts,
        request: RequestBase,
        session: SessionId,
        invocation_id: str | None,
    ) -> ObservationFacts:
        """Judge the writes of every invocation this released outcome proves ended.

        *invocation_id* narrows it to one invocation, otherwise every dispatched
        invocation of the session whose journal row proves its writer ended. An
        outcome that is not released proves nothing and is returned unchanged; one
        whose revert cannot finish is Unknown, so a retry resumes it.
        """
        if not facts.released:
            return facts
        bkey = session_binding_key(request, session)
        binding = load_session_binding(self._store, bkey)
        if binding is None:
            return facts
        try:
            await self._settle_ended(
                bkey, binding, invocation_id, closed=isinstance(request, CloseSession)
            )
        except AccessSettlementError as error:
            return _unknown(str(error), binding)
        except (ReceiptCorruptError, *_SESSION_ERRORS) as error:
            return _unknown(f"access cannot be settled: {error}", binding)
        return facts

    async def _settle_ended(
        self, bkey: str, binding: SessionBinding, only: str | None, *, closed: bool = False
    ) -> None:
        """Settle the receipts of the session's invocations whose writers provably ended.

        A closed session also ends every turn whose dispatch call returned: no
        provider resource of the key remains.
        """
        key = AgentSessionKey.parse(binding.session_key)
        for invocation_id in binding.dispatched if only is None else (only,):
            access_key = AccessKey(binding=bkey, invocation=invocation_id)
            receipt = self._settlement.receipt(access_key)
            if receipt is None or receipt.settled:
                continue
            row = await self._blocking.run(_journal_row, self._sessions, key, invocation_id)
            if (_call_returned if closed else _writer_ended)(row):
                await self._settlement.settle(access_key)

    # cancel

    def _cancel(self, request: CancelTurn, context: ExecutionContext) -> ObservationFacts:
        """Close the invocation's fence, then stop and judge whatever its dispatches began."""
        invocation = request.invocation
        bkey = session_binding_key(request, invocation.session_id)
        fence = invocation_fence(bkey, invocation.invocation_id.root)
        with self._store.exclusive():
            closure = self._fences.close(
                fence,
                by=core_identity(request),
                host=context.fence,
            )
            binding = self._binding(request, invocation.session_id)
        if isinstance(closure, NothingHeld):
            # No dispatch entered the fence, and none can now: no provider call ever begins.
            return ObservationFacts.never_held(
                closure, fence, "the invocation was never dispatched"
            )
        if binding is None:
            return _unknown("the invocation was dispatched without a session binding", None)
        return self._stop(closure, binding, invocation.invocation_id.root)

    def _stop(
        self, closure: MayHold, binding: SessionBinding, invocation_id: str
    ) -> ObservationFacts:
        """Stop the turn this instance runs, then judge it from its journal row."""
        key = AgentSessionKey.parse(binding.session_key)
        try:
            self._sessions.cancel(key, invocation_id)
            record = _journal_row(self._sessions, key, invocation_id)
            if record is None:
                return self._never_journaled(closure, binding)
            if isinstance(record.outcome, Pending):
                # Pending: the dispatch call has not returned, here or in an earlier
                # host. Only this instance can reach a turn it runs, so a restarted
                # host cannot prove the provider process stopped.
                return _unknown(
                    "the turn is not proven stopped: its dispatch has not returned", binding
                )
            if isinstance(record.outcome, (Completed, InvalidResponse)):
                return _released(
                    ObservationStatus.CANCELLED,
                    binding,
                    "the turn ended before the cancellation took effect",
                )
            # Unknown was recorded by a dispatch call that returned, so the provider turn
            # of that call is over. release_interrupted still refuses an active invocation.
            self._sessions.release_interrupted(key, invocation_id)
        except _SESSION_ERRORS as error:
            return _unknown(f"the invocation cannot be settled: {error}", binding)
        return _released(ObservationStatus.CANCELLED, binding)

    def _never_journaled(self, closure: MayHold, binding: SessionBinding) -> ObservationFacts:
        """A dispatched invocation with no journal row: no provider call began, yet.

        The dispatch intent is journaled before any provider call. A dispatch that entered
        the fence and has not left may still journal and call; one that left, or whose host
        a restart replaced, never will, because the closed fence refuses its next entry.
        """
        if closure.live:
            return _unknown("a dispatch of the turn is in flight and may still start", binding)
        # Every dispatch ended without a provider call (refused, or its host replaced), and
        # none can start any more: the cancel ended the turn.
        return _released(
            ObservationStatus.CANCELLED, binding, "the turn was cancelled before it began"
        )

    # close

    def _close(self, request: CloseSession, context: ExecutionContext) -> ObservationFacts:
        """Close the episode's fence, then release the session an ensure bound, if any."""
        bkey = session_binding_key(request, request.session_id)
        fence = episode_fence(bkey, request.episode)
        closure, binding = self._fences.close_and_load(
            fence,
            by=core_identity(request),
            host=context.fence,
            load=lambda: load_session_binding(self._store, bkey),
        )
        if isinstance(binding, Unrecorded):
            # An ensure binds in the same locked step as it enters, so an entered fence
            # without a binding only has entries that refused: nothing was bound.
            if isinstance(closure, MayHold) and closure.live:
                return _unknown("an ensure of the session is in flight")
            proof = (
                closure
                if isinstance(closure, NothingHeld)
                else self._fences.cleared(closure, binding)
            )
            return ObservationFacts.never_held(proof, fence, "no ensured session to close")
        try:
            self._sessions.release(AgentSessionKey.parse(binding.session_key))
        except InvocationConflictError as error:
            return _unknown(f"an invocation is still active: cancel it first ({error})", binding)
        except (SessionConfigurationError, SessionPersistenceError) as error:
            return _unknown(f"the conversation cannot be released: {error}", binding)
        # The binding and the provider checkpoint stay: a later EnsureSession reattaches
        # the conversation and ResumeSessionTurn continues it.
        return _released(ObservationStatus.SUCCEEDED, binding)

    # resume

    async def attempt(
        self, request: ResumeSessionTurn, context: ExecutionContext, *, mark: str
    ) -> ExecutionResult:
        """Run one attempt of a resume and return what it observed, recording no receipt of it.

        The step loop owns the step's result. The continuation binding is written once and an
        attempt that repeats finds it; the nested dispatch is ``RuntimeSessionRequests.attempt``,
        so a rerun after a killed attempt is dispatched again once the runner has cleared it.
        The turn's agent processes carry *mark*.
        """
        request_id = core_identity(request)
        dispatch = partial(self._attempt_resumed, mark=mark)
        return await self._resume(request, request_id, context, dispatch)

    async def _dispatch_resumed(
        self, dispatch: DispatchTurn, behalf: CoreRequestId, context: ExecutionContext
    ) -> ExecutionOutcome:
        return await self._dispatcher.dispatch_on_behalf_of(dispatch, behalf, context)

    async def _attempt_resumed(
        self, dispatch: DispatchTurn, behalf: CoreRequestId, context: ExecutionContext, *, mark: str
    ) -> ExecutionOutcome:
        return await self._dispatcher.attempt(dispatch, context, mark=mark, behalf=behalf)

    async def _resume(
        self,
        request: ResumeSessionTurn,
        request_id: RequestId,
        context: ExecutionContext,
        dispatch_turn: ResumedDispatch,
    ) -> ExecutionResult:
        turn = request.turn
        if turn.charge_class != "resume":
            return self._result(
                request,
                context,
                ObservationFacts.refused("a resume turn needs charge class resume"),
            )
        if turn.continuation_id != request.continuation_id:
            return self._result(
                request,
                context,
                ObservationFacts.refused("the turn names another continuation than the request"),
            )
        problem = await self._blocking.run(self._bind_continuation, request)
        if problem is not None:
            return self._result(request, context, _refused_before_dispatch(problem))
        superseded = await self._settle_superseded(request)
        if superseded is not None:
            return self._result(request, context, superseded)
        # The nested step's own id only keys its receipt; the dispatch record, which
        # inspection answers from, names the resume request core knows.
        dispatch = DispatchTurn(
            request_id=RequestId(root=f"{request_id.root}.dispatch"),
            scope=request.scope,
            depends_on=request.depends_on,
            decision_id=request.decision_id,
            admission_id=request.admission_id,
            turn=turn,
            inputs=request.inputs,
            guidance=request.guidance,
        )
        outcome = await dispatch_turn(dispatch, core_identity(request), context)
        if not isinstance(outcome, ExecutionResult):
            return self._result(request, context, _unknown(outcome.detail))
        seen = outcome.observation.observation
        event = next((e for e in outcome.owner_events if isinstance(e, TurnObserved)), None)
        facts = (
            # The nested dispatch refused itself, sealed under its identity: so does the resume.
            ObservationFacts.refused(seen.diagnostic)
            if seen.terminal and seen.released and not seen.accepted and seen.resource_id is None
            else ObservationFacts(
                status=seen.status,
                terminal=seen.terminal,
                accepted=seen.accepted,
                released=seen.released,
                children_complete=seen.children_complete,
                resource_id=seen.resource_id,
                diagnostic=seen.diagnostic,
            )
        )
        return self._result(request, context, facts, turn=event)

    async def _settle_superseded(self, request: ResumeSessionTurn) -> ObservationFacts | None:
        """Judge the writes of earlier turns the successor supersedes, before it can snapshot.

        Only turns whose writers provably ended qualify; the resumed dispatch then
        refuses to baseline a workspace that still holds an unjudged one.
        """
        session = request.turn.session.session_id
        bkey = session_binding_key(request, session)
        binding = load_session_binding(self._store, bkey)
        if binding is None:
            return None
        try:
            await self._settle_ended(bkey, binding, None)
        except AccessSettlementError as error:
            return _unknown(str(error), binding)
        except (ReceiptCorruptError, *_SESSION_ERRORS) as error:
            return _unknown(f"access cannot be settled: {error}", binding)
        return None

    def _retained(
        self, request: ResumeSessionTurn
    ) -> tuple[SessionBinding, AgentSessionCheckpoint] | ObservationFacts:
        """The session's binding and retained provider conversation, or why there is none."""
        session = request.turn.session
        binding = self._binding(request, session.session_id)
        if binding is None or binding.spec != session.model_copy(
            update={"policy": binding.spec.policy}
        ):
            return _rejected("no ensured session matches this turn's session")
        if not conversation_established(self._sessions, binding):
            return _rejected("the session completed no turn: there is nothing to resume", binding)
        try:
            checkpoint = self._sessions.checkpoint(AgentSessionKey.parse(binding.session_key))
        except SessionResumeError as error:
            return _rejected(f"no retained checkpoint to continue: {error}", binding)
        except (SessionConfigurationError, SessionPersistenceError) as error:
            return _unknown(f"cannot read the retained checkpoint: {error}", binding)
        return binding, checkpoint

    def _bind_continuation(self, request: ResumeSessionTurn) -> ObservationFacts | None:
        """Fix the continuation to one successor and one conversation, or say why not."""
        retained = self._retained(request)
        if isinstance(retained, ObservationFacts):
            return retained
        binding, checkpoint = retained
        wanted = ContinuationBinding(
            invocation_id=request.turn.invocation_id.root,
            provider_session_id=checkpoint.provider_session_id,
        )

        def decide(
            stored: ContinuationBinding | None,
        ) -> tuple[ContinuationBinding | None, ContinuationBinding]:
            return (wanted, wanted) if stored is None else (None, stored)

        bound = self._store.modify(
            _CONTINUATIONS,
            "binding",
            f"{owner_key(request)}/{request.continuation_id.root}",
            ContinuationBinding,
            decide,
        )
        if bound.invocation_id != wanted.invocation_id:
            return _rejected("the continuation already has another successor invocation", binding)
        if bound.provider_session_id != wanted.provider_session_id:
            return _rejected(
                "the retained conversation is not the one this continuation was bound to", binding
            )
        return None


class SessionRequestRouter:
    """The SESSIONS executor: lifecycle kinds go to B, the rest to the turn dispatcher."""

    def __init__(self, turns: TurnDispatcher, lifecycle: SessionLifecycleRequests) -> None:
        """Bind the Ensure/Dispatch/Inspect executor and the lifecycle executor."""
        self._turns = turns
        self._lifecycle = lifecycle

    async def execute(
        self, request: SessionRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome:
        """Route one request by kind."""
        target = self._lifecycle if isinstance(request, _HANDLED) else self._turns
        return await target.execute(request, context)


__all__ = [
    "ContinuationBinding",
    "ReleasedRunInvocations",
    "SessionLifecycleRequests",
    "SessionRequestRouter",
    "TurnDispatcher",
]
