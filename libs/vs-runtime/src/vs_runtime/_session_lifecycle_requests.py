"""Session lifecycle executors: CancelTurn, CloseSession and ResumeSessionTurn.

They sit beside ``RuntimeSessionRequests`` (EnsureSession, DispatchTurn, InspectTurn)
and read its durable tables: the ``SessionBinding`` of a session (spec, conversation
key, resource) and the ``DispatchRecord`` of an invocation. Each request runs at most
once per canonical identity through the shared ``ReceiptStore`` (begun marker, fence
and lease check, sealed result) and reports through the shared ``ObservationFactory``.

Mechanisms:

* **CancelTurn** never assumes a stop. It asks ``AgentSessions.cancel`` to stop the
  turn this instance runs, then reads the invocation's journal row. A ``Pending`` row
  means the dispatch call has not returned, here or in an earlier host that a restart
  replaced: Unknown, because a new host cannot prove the old provider process
  stopped. A ``Completed`` or ``InvalidResponse`` row shows the turn ended. An
  ``Unknown`` row was written by a dispatch call that returned, so the key is
  released with ``release_interrupted``, which itself refuses an active invocation.
  An invocation that was never dispatched is REJECTED.
* **CloseSession** releases the key's live provider resources with
  ``AgentSessions.release`` and keeps the binding and the provider checkpoint, so a
  reused conversation is reattached and resumed later. An active invocation makes it
  Unknown: the shell cancels first.
* **ResumeSessionTurn** continues the retained conversation and never substitutes a
  fresh one. The first resume of a continuation binds it to one successor invocation
  and to the retained provider conversation; another successor, or another
  conversation under the same continuation, is REJECTED. The dispatch is a
  ``DispatchTurn`` of the same turn under a derived request identity, so it gets the
  write-ahead journal, replay of a recorded outcome and Unknown-never-repeated of
  ``RuntimeSessionRequests``; its facts are re-observed under the resume request.

The executor never returns ``ExecutorRefusal``: every inability is a typed
observation. Only terminal observations are stored.
"""

from __future__ import annotations

import asyncio
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
from vs_runtime._core_requests import ExecutionContext, ExecutionOutcome, ExecutionResult
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
    load_dispatch_record,
    load_session_binding,
    session_binding_key,
)

if TYPE_CHECKING:
    from vs_agent.api import AgentInvocationRecord, AgentSessionCheckpoint, AgentSessions
    from vs_core.api import RequestBase, SessionId, SnapshotAndRetainRun
    from vs_runtime._core_requests import OwnerEvent, SessionRoleRequest
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._workspace_requests import RunInvocationProof

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
    ) -> None:
        """Bind the durable agent sessions, the turn dispatcher, the store and its settlement.

        *settlement* is the instance the turn dispatcher records receipts with.
        """
        self._sessions = sessions
        self._dispatcher = dispatcher
        self._store = store
        self._settlement = settlement
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
                return self._result(
                    request, context, _rejected("same request identity with another payload")
                )
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
                facts = await asyncio.to_thread(self._cancel, request)
                facts = await self._settled(
                    facts,
                    request,
                    request.invocation.session_id,
                    request.invocation.invocation_id.root,
                )
            case CloseSession():
                facts = await asyncio.to_thread(self._close, request)
                facts = await self._settled(facts, request, request.session_id, None)
            case ResumeSessionTurn():
                return await self._resume(request, request_id, context)
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
            await self._settle_ended(bkey, binding, invocation_id)
        except AccessSettlementError as error:
            return _unknown(str(error), binding)
        except (ReceiptCorruptError, *_SESSION_ERRORS) as error:
            return _unknown(f"access cannot be settled: {error}", binding)
        return facts

    async def _settle_ended(self, bkey: str, binding: SessionBinding, only: str | None) -> None:
        """Settle the receipts of the session's invocations whose writers provably ended."""
        key = AgentSessionKey.parse(binding.session_key)
        for invocation_id in binding.dispatched if only is None else (only,):
            access_key = AccessKey(binding=bkey, invocation=invocation_id)
            receipt = self._settlement.receipt(access_key)
            if receipt is None or receipt.settled:
                continue
            row = await asyncio.to_thread(_journal_row, self._sessions, key, invocation_id)
            if _writer_ended(row):
                await self._settlement.settle(access_key)

    # cancel

    def _cancel(self, request: CancelTurn) -> ObservationFacts:
        invocation = request.invocation
        binding = self._binding(request, invocation.session_id)
        if binding is None:
            return _rejected("no ensured session for this invocation")
        link = load_dispatch_record(
            self._store,
            session_binding_key(request, invocation.session_id),
            invocation.invocation_id,
        )
        if link is None:
            return _rejected("the invocation was never dispatched", binding)
        key = AgentSessionKey.parse(binding.session_key)
        invocation_id = invocation.invocation_id.root
        try:
            self._sessions.cancel(key, invocation_id)
            record = _journal_row(self._sessions, key, invocation_id)
            if record is None or isinstance(record.outcome, Pending):
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

    # close

    def _close(self, request: CloseSession) -> ObservationFacts:
        binding = self._binding(request, request.session_id)
        if binding is None:
            return _rejected("no ensured session to close")
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

    async def _resume(
        self, request: ResumeSessionTurn, request_id: RequestId, context: ExecutionContext
    ) -> ExecutionResult:
        turn = request.turn
        if turn.charge_class != "resume":
            return self._result(
                request, context, _rejected("a resume turn needs charge class resume")
            )
        if turn.continuation_id != request.continuation_id:
            return self._result(
                request, context, _rejected("the turn names another continuation than the request")
            )
        problem = await asyncio.to_thread(self._bind_continuation, request)
        if problem is not None:
            return self._result(request, context, problem)
        superseded = await self._settle_superseded(request)
        if superseded is not None:
            return self._result(request, context, superseded)
        dispatch = DispatchTurn(
            request_id=RequestId(root=f"{request_id.root}.dispatch"),
            scope=request.scope,
            depends_on=request.depends_on,
            decision_id=request.decision_id,
            admission_id=request.admission_id,
            decision_dependencies=request.decision_dependencies,
            deadline_at=request.deadline_at,
            turn=turn,
            inputs=request.inputs,
        )
        outcome = await self._dispatcher.execute(dispatch, context)
        if not isinstance(outcome, ExecutionResult):
            return self._result(request, context, _unknown(outcome.detail))
        seen = outcome.observation.observation
        event = next((e for e in outcome.owner_events if isinstance(e, TurnObserved)), None)
        facts = ObservationFacts(
            status=seen.status,
            terminal=seen.terminal,
            accepted=seen.accepted,
            resource_id=seen.resource_id,
            diagnostic=seen.diagnostic,
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
