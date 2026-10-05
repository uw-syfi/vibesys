"""Session request executors: EnsureSession, DispatchTurn and InspectTurn.

Core names sessions by ``SessionId`` and turns by ``InvocationRef``. This module
maps them onto durable ``vs_agent`` conversations (``AgentSessions``) and
translates what the journal proves back into core observations.

Rules every kind follows (the shared ``ReceiptStore.run_once`` enforces the first
four; see ``_receipt_store``):

* a sealed result is replayed, never recomputed;
* another payload under one request identity is a REJECTED observation;
* a stale lease or host fence performs no effect and reports Unknown;
* a ``begun`` marker is written before the effect, so a restarted effect inspects;
* an existing conversation is reattached positively. A missing binding or a lost
  provider checkpoint is a typed rejection; a fresh conversation never takes its
  place, whatever the reuse policy says.

Identity maps. ``SessionBinding`` (one durable receipt per scope owner, generation
and session id) fixes the session's spec, its provider conversation key and its
resource id. ``DispatchRecord`` (one per invocation) correlates an invocation with
the request that dispatched it, written before any provider call, so its absence
proves a turn was never dispatched. The ``vs_agent`` invocation journal is the
sole authority for what a dispatched turn did; nothing here repeats a dispatch it
cannot prove unaccepted, because ``AgentSessions.start`` and ``resume`` return the
journal's outcome for a known invocation instead of dispatching again.

Outcome mapping: Completed is SUCCEEDED and terminal; an invalid response is a
terminal FAILED observation of an accepted turn; Pending and Unknown are UNKNOWN
and never sealed. The executor never returns ``ExecutorRefusal``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, assert_never

from pydantic import BaseModel, ConfigDict

from vs_agent.api import (
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentTurnRequest,
    Completed,
    InvalidResponse,
    InvocationConflictError,
    Pending,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
    Unknown,
    parse_typed_response,
)
from vs_core.api import (
    ContractError,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    InvocationRef,
    ObservationStatus,
    RequestId,
    RequestObserved,
    ResourceId,
    RoleId,
    SchemaRef,
    SessionId,
    SessionInput,
    SessionObserved,
    SessionSpec,
    SetupFailureKind,
    SnapshotAndRetainRun,
    TargetObservation,
    TurnObserved,
    TurnSpec,
)
from vs_runtime._agent_sessions import await_session_operation
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

if TYPE_CHECKING:
    from vs_agent.api import AgentSessionSpec, ClientAgentSessions, InvocationOutcome
    from vs_core.api import Observation, RequestBase
    from vs_prompts.api import RenderedPrompt
    from vs_runtime._core_requests import OwnerEvent, SessionRoleRequest
    from vs_runtime._receipt_store import ReceiptStore

_BINDINGS = "session-bindings"
_DISPATCHES = "session-dispatches"


class SessionResolver(Protocol):
    """What the executor needs from the run to turn core declarations into agent inputs.

    Every method answers ``None`` (or False) for something the run does not know;
    the executor then rejects the request and names the unknown item.
    """

    def knows_role(self, role: RoleId) -> bool:
        """Whether *role* is a declared agent role of this run."""
        ...

    def agent_spec(self, turn: TurnSpec) -> AgentSessionSpec | None:
        """The provider session configuration for *turn* (role, workspace, policy)."""
        ...

    def output_schema(self, ref: SchemaRef) -> type[BaseModel] | None:
        """The pydantic type that validates replies for *ref*."""
        ...

    def template(self, turn: TurnSpec) -> AgentTurnRequest | None:
        """The fixed per-role turn configuration: instructions, timeout, constant label.

        These fields must not vary between turns of one conversation, because the
        durable session fences them. The executor adds message, schema and identity.
        """
        ...

    def message(self, turn: TurnSpec, inputs: tuple[SessionInput, ...]) -> RenderedPrompt | None:
        """The rendered turn message from the turn's prompts and reserved inputs.

        Rendering is the resolver's job (templates); this executor never builds
        prompt text. ``None`` means an artifact is missing or fails its digest.
        """
        ...


class SessionBinding(BaseModel):
    """Durable identity of one core session: spec, conversation key and resource."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    spec: SessionSpec
    ensure_request: RequestId
    resource_id: ResourceId
    session_key: str
    established: bool = False
    """True once a turn completed, so the provider conversation must exist."""


class DispatchRecord(BaseModel):
    """Written before the provider is called: which request dispatches an invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: RequestId
    digest: str
    output_schema: SchemaRef


@dataclass(frozen=True)
class _Facts:
    status: ObservationStatus
    terminal: bool = True
    accepted: bool = False
    resource_id: ResourceId | None = None
    diagnostic: str = ""
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN
    output_schema: SchemaRef | None = None
    output_json: str | None = None


def _same_session(bound: SessionSpec, asked: SessionSpec) -> bool:
    """Same session identity, role, lifetime and access; the policy only says how to acquire it."""
    return bound == asked.model_copy(update={"policy": bound.policy})


def _rejected(diagnostic: str, resource_id: ResourceId | None = None) -> _Facts:
    return _Facts(
        ObservationStatus.REJECTED,
        resource_id=resource_id,
        diagnostic=diagnostic,
        setup_failure=SetupFailureKind.PERMANENT,
    )


def _unknown(diagnostic: str, resource_id: ResourceId | None = None) -> _Facts:
    return _Facts(
        ObservationStatus.UNKNOWN, terminal=False, resource_id=resource_id, diagnostic=diagnostic
    )


@dataclass(frozen=True)
class _Dispatch:
    """The resolved inputs of one provider dispatch."""

    key: AgentSessionKey
    spec: AgentSessionSpec
    template: AgentTurnRequest
    message: RenderedPrompt
    invocation: str
    schema: type[BaseModel]


class _RefusalError(Exception):
    """A validation step ended the request with these terminal or unknown facts."""

    def __init__(self, facts: _Facts) -> None:
        super().__init__(facts.diagnostic)
        self.facts = facts


@dataclass(frozen=True)
class _Call:
    """One request with the identity core guarantees it carries."""

    request: EnsureSession | DispatchTurn
    request_id: RequestId
    context: ExecutionContext


class RuntimeSessionRequests:
    """Translate session requests into durable ``ClientAgentSessions`` calls, once each."""

    def __init__(
        self, sessions: ClientAgentSessions, resolver: SessionResolver, store: ReceiptStore
    ) -> None:
        """Bind the durable agent sessions and the run's resolver to the shared store."""
        self._sessions = sessions
        self._resolver = resolver
        self._store = store
        self._observations = ObservationFactory(store)
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self, request: SessionRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome:
        """Execute, replay or inspect one request under its canonical identity."""
        if request.request_id is None:
            message = "request_id: execution requires a canonical identity"
            raise ValueError(message)
        if isinstance(request, InspectTurn):
            return self._inspect(request, context)
        if not isinstance(request, (EnsureSession, DispatchTurn)):
            return self._result(request, context, _rejected(f"{request.kind} is not executed here"))
        call = _Call(request, request.request_id, context)
        lock_key = f"{owner_key(request)}/{self._session_id(request).root}"
        async with self._locks.setdefault(lock_key, asyncio.Lock()):
            return await self._run(call)

    async def _run(self, call: _Call) -> ExecutionOutcome:
        request, context = call.request, call.context

        async def perform(
            *, resumed: bool
        ) -> Settled[ExecutionResult] | Transient[ExecutionResult]:
            del resumed  # the journal, not this flag, says whether a dispatch happened
            try:
                facts = await self._perform(call)
            except _RefusalError as refusal:
                facts = refusal.facts
            except ReceiptCorruptError as error:
                facts = _unknown(str(error))
            result = self._result(request, context, facts)
            return Settled(result) if result.observation.observation.terminal else Transient(result)

        execution = await self._store.run_once(
            call.request_id.root,
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

    @staticmethod
    def _session_id(request: EnsureSession | DispatchTurn) -> SessionId:
        if isinstance(request, EnsureSession):
            return request.spec.session_id
        return request.turn.session.session_id

    # translation to core events

    def _result(
        self, request: RequestBase, context: ExecutionContext, facts: _Facts
    ) -> ExecutionResult:
        observation = self._observations.observe(
            ObservationSubject.of(request),
            ObservationFacts(
                status=facts.status,
                terminal=facts.terminal,
                accepted=facts.accepted,
                resource_id=facts.resource_id,
                diagnostic=facts.diagnostic,
            ),
            observed_at=context.now_at,
        )
        events: tuple[OwnerEvent, ...] = ()
        if isinstance(request, EnsureSession):
            events = (
                SessionObserved(
                    session_id=request.spec.session_id,
                    observation=observation,
                    failure=facts.setup_failure,
                ),
            )
        elif isinstance(request, DispatchTurn):
            invocation = InvocationRef(
                session_id=request.turn.session.session_id,
                invocation_id=request.turn.invocation_id,
                generation=request.scope.generation,
            )
            events = (self._turn_event(invocation, observation, facts),)
        return ExecutionResult(
            observation=RequestObserved(
                observation=observation,
                setup_failure=facts.setup_failure,
                outcome_schema=facts.output_schema,
                outcome_json=facts.output_json,
            ),
            owner_events=events,
        )

    @staticmethod
    def _turn_event(
        invocation: InvocationRef, observation: Observation, facts: _Facts
    ) -> TurnObserved:
        return TurnObserved(
            invocation=invocation,
            observation=observation,
            output_schema=facts.output_schema,
            output_json=facts.output_json,
        )

    # EnsureSession and DispatchTurn

    async def _perform(self, call: _Call) -> _Facts:
        if isinstance(call.request, EnsureSession):
            return await self._ensure(call.request, call.request_id)
        return await self._dispatch(call.request, call)

    @staticmethod
    def _binding_key(request: RequestBase, session_id: SessionId) -> str:
        return f"{owner_key(request)}/{session_id.root}"

    @staticmethod
    def _conversation_key(request: RequestBase, session: SessionSpec) -> str:
        owner = request.scope.owner
        member = f"{owner.kind}:{owner.root}:{session.session_id.root}"
        return str(
            AgentSessionKey.for_member(
                session.role_id.root, member, generation=request.scope.generation + 1
            )
        )

    async def _ensure(self, request: EnsureSession, request_id: RequestId) -> _Facts:
        spec = request.spec
        if not self._resolver.knows_role(spec.role_id):
            return _rejected(f"role {spec.role_id.root} is not declared")
        key = self._binding_key(request, spec.session_id)
        required = request.required_resource
        wanted = SessionBinding(
            spec=spec,
            ensure_request=request_id,
            resource_id=ResourceId(root=f"session:{key}"),
            session_key=self._conversation_key(request, spec),
        )
        binding = self._bind(key, wanted, required)
        if not _same_session(binding.spec, spec):
            raise _RefusalError(
                _rejected("session identity is bound to another spec", binding.resource_id)
            )
        if required is not None and required != binding.resource_id:
            raise _RefusalError(
                _rejected("required resource is not this session's lease", binding.resource_id)
            )
        if binding.ensure_request != request_id and spec.policy == "fresh":
            raise _RefusalError(
                _rejected("session identity is already in use", binding.resource_id)
            )
        if binding.established:
            await self._require_checkpoint(binding)
        return _Facts(ObservationStatus.SUCCEEDED, accepted=True, resource_id=binding.resource_id)

    def _bind(
        self, key: str, wanted: SessionBinding, required: ResourceId | None
    ) -> SessionBinding:
        """The session's binding: created for a fresh identity, never for a required reattach."""
        if required is not None and wanted.spec.policy != "reuse":
            raise _RefusalError(_rejected("a required resource needs the reuse policy", required))

        def decide(stored: SessionBinding | None) -> tuple[SessionBinding | None, SessionBinding]:
            if stored is not None:
                return None, stored
            if required is not None:
                raise _RefusalError(
                    _rejected(
                        "no durable correspondence for the required session resource; "
                        "a fresh conversation will not replace it",
                        required,
                    )
                )
            return wanted, wanted

        return self._store.modify(_BINDINGS, "binding", key, SessionBinding, decide)

    async def _require_checkpoint(self, binding: SessionBinding) -> None:
        """Raise unless the provider conversation of an established session is present."""
        key = AgentSessionKey.parse(binding.session_key)
        try:
            await asyncio.to_thread(self._sessions.checkpoint, key)
        except SessionResumeError as error:
            raise _RefusalError(
                _rejected(
                    "the provider conversation checkpoint is missing; "
                    "a fresh conversation will not replace it",
                    binding.resource_id,
                )
            ) from error
        except (SessionPersistenceError, SessionConfigurationError) as error:
            raise _RefusalError(
                _unknown(f"cannot read the conversation checkpoint: {error}", binding.resource_id)
            ) from error

    def _resolve(self, request: DispatchTurn, bkey: str) -> tuple[SessionBinding, _Dispatch]:
        """The ensured session and the resolved dispatch, or a refusal naming what is missing."""
        turn = request.turn
        binding = self._store.load(_BINDINGS, "binding", bkey, SessionBinding)
        if binding is None or not _same_session(binding.spec, turn.session):
            raise _RefusalError(_rejected("no ensured session matches this turn's session"))
        schema = self._resolver.output_schema(turn.output_schema)
        if schema is None:
            raise _RefusalError(
                _rejected(f"output schema {turn.output_schema.name} is not registered")
            )
        spec = self._resolver.agent_spec(turn)
        template = self._resolver.template(turn)
        message = self._resolver.message(turn, request.inputs)
        if spec is None or template is None or message is None:
            raise _RefusalError(
                _rejected("the turn's workspace, prompts or inputs cannot be resolved")
            )
        dispatch = _Dispatch(
            AgentSessionKey.parse(binding.session_key),
            spec,
            replace(template, output_schema=schema),
            message,
            turn.invocation_id.root,
            schema,
        )
        return binding, dispatch

    async def _dispatch(self, request: DispatchTurn, call: _Call) -> _Facts:
        turn = request.turn
        bkey = self._binding_key(request, turn.session.session_id)
        binding, dispatch = self._resolve(request, bkey)
        if turn.deadline_at <= call.context.now_at:
            return _rejected("the turn deadline passed before dispatch", binding.resource_id)
        link = DispatchRecord(
            request_id=call.request_id,
            digest=call.context.payload_digest,
            output_schema=turn.output_schema,
        )
        try:
            # Written before any provider call: its absence later proves no dispatch began.
            self._store.record_once(_DISPATCHES, "dispatch", f"{bkey}/{dispatch.invocation}", link)
        except ContractError:
            return _rejected("invocation is dispatched by another request", binding.resource_id)
        if binding.established:
            await self._require_checkpoint(binding)
        try:
            outcome = await await_session_operation(
                asyncio.create_task(
                    asyncio.to_thread(
                        self._start_or_resume, dispatch, established=binding.established
                    )
                )
            )
        except InvocationConflictError as error:
            return _rejected(f"invocation conflicts with its journal: {error}", binding.resource_id)
        except SessionConfigurationError as error:
            return _rejected(f"session configuration refused: {error}", binding.resource_id)
        except (SessionPersistenceError, SessionResumeError) as error:
            return _unknown(f"dispatch state is unreadable: {error}", binding.resource_id)
        facts = self._translate(outcome, binding, dispatch.schema, turn.output_schema)
        if facts.status is ObservationStatus.SUCCEEDED and not binding.established:
            self._store.replace(
                _BINDINGS, "binding", bkey, binding.model_copy(update={"established": True})
            )
        return facts

    def _start_or_resume(self, dispatch: _Dispatch, *, established: bool) -> InvocationOutcome:
        """Continue an established conversation; start only one that never completed a turn."""
        sessions = self._sessions
        key, invocation = dispatch.key, dispatch.invocation
        if not established:
            turn = replace(dispatch.template, message=dispatch.message, invocation_id=invocation)
            return sessions.start(key, dispatch.spec, turn)
        previous = sessions.inspect(key, invocation)
        checkpoint = previous.checkpoint or sessions.checkpoint(key)
        sessions.bind(
            key,
            dispatch.spec,
            replace(dispatch.template, expected_provider_session_id=checkpoint.provider_session_id),
        )
        return sessions.resume(key, dispatch.message, invocation)

    @staticmethod
    def _translate(
        outcome: InvocationOutcome,
        binding: SessionBinding,
        schema: type[BaseModel],
        ref: SchemaRef,
    ) -> _Facts:
        resource = binding.resource_id
        match outcome:
            case Completed():
                try:
                    reply = parse_typed_response(outcome.result.text, schema)
                except AgentOutputSchemaError as error:
                    return _Facts(
                        ObservationStatus.FAILED,
                        accepted=True,
                        resource_id=resource,
                        diagnostic=f"reply fails its schema: {error.detail}",
                    )
                return _Facts(
                    ObservationStatus.SUCCEEDED,
                    accepted=True,
                    resource_id=resource,
                    output_schema=ref,
                    output_json=reply.model_dump_json(),
                )
            case InvalidResponse():
                return _Facts(
                    ObservationStatus.FAILED,
                    accepted=True,
                    resource_id=resource,
                    diagnostic=outcome.detail,
                )
            case Pending():
                return _unknown("the turn is in flight", resource)
            case Unknown():
                return _unknown(outcome.detail, resource)
            case _:
                assert_never(outcome)

    # InspectTurn

    def _inspect(self, request: InspectTurn, context: ExecutionContext) -> ExecutionOutcome:
        """Translate the correlated durable facts of one invocation; nothing is dispatched."""
        try:
            return self._inspect_facts(request, context)
        except _RefusalError as refusal:
            return self._result(request, context, refusal.facts)
        except ReceiptCorruptError as error:
            return self._result(request, context, _unknown(str(error)))

    def _inspect_facts(self, request: InspectTurn, context: ExecutionContext) -> ExecutionOutcome:
        invocation = request.invocation
        bkey = self._binding_key(request, invocation.session_id)
        binding = self._store.load(_BINDINGS, "binding", bkey, SessionBinding)
        if binding is None:
            raise _RefusalError(_rejected("no ensured session for this invocation"))
        link = self._store.load(
            _DISPATCHES, "dispatch", f"{bkey}/{invocation.invocation_id.root}", DispatchRecord
        )
        if link is None:
            # The link is written before any provider call: no link means no dispatch began.
            target = _Facts(
                ObservationStatus.FAILED,
                resource_id=binding.resource_id,
                diagnostic="the invocation was never dispatched",
            )
            return self._inspected(request, context, target, request.request_id)
        schema = self._resolver.output_schema(link.output_schema)
        if schema is None:
            raise _RefusalError(
                _rejected(f"output schema {link.output_schema.name} is not registered")
            )
        key = AgentSessionKey.parse(binding.session_key)
        try:
            outcome = self._sessions.inspect(key, invocation.invocation_id.root)
        except (
            SessionPersistenceError,
            SessionConfigurationError,
            InvocationConflictError,
        ) as error:
            raise _RefusalError(_unknown(str(error), binding.resource_id)) from error
        target = self._translate(outcome, binding, schema, link.output_schema)
        return self._inspected(request, context, target, link.request_id)

    def _inspected(
        self,
        request: InspectTurn,
        context: ExecutionContext,
        target: _Facts,
        target_request: RequestId | None,
    ) -> ExecutionResult:
        """The query's own success, carrying the target's observation on its own sequence."""
        own = self._observations.observe(
            ObservationSubject.of(request),
            ObservationFacts(
                status=ObservationStatus.SUCCEEDED, accepted=True, resource_id=target.resource_id
            ),
            observed_at=context.now_at,
        )
        seen = self._observations.observe(
            ObservationSubject.of(request, request_id=target_request),
            ObservationFacts(
                status=target.status,
                terminal=target.terminal,
                accepted=target.accepted,
                resource_id=target.resource_id,
                diagnostic=target.diagnostic,
            ),
            observed_at=context.now_at,
        )
        return ExecutionResult(
            observation=RequestObserved(
                observation=own,
                target=TargetObservation(
                    observation=seen,
                    setup_failure=target.setup_failure,
                    outcome_schema=target.output_schema,
                    outcome_json=target.output_json,
                ),
            ),
            owner_events=(self._turn_event(request.invocation, seen, target),),
        )


class JournalRunInvocations:
    """Run-invocation proof from the session journal: only a settled turn has no live writer.

    Completed and schema-rejected turns ended. Never-dispatched, in-flight and
    Unknown turns prove nothing, because the provider may still be writing.
    """

    def __init__(self, sessions: ClientAgentSessions, store: ReceiptStore) -> None:
        """Read the same journal and bindings the session executor writes."""
        self._sessions = sessions
        self._store = store

    def unproven(self, request: SnapshotAndRetainRun) -> str | None:
        """None when the invocation's turn settled; otherwise why its writer is not proven gone."""
        invocation = request.invocation
        bkey = f"{owner_key(request)}/{invocation.session_id.root}"
        try:
            binding = self._store.load(_BINDINGS, "binding", bkey, SessionBinding)
            link = self._store.load(
                _DISPATCHES, "dispatch", f"{bkey}/{invocation.invocation_id.root}", DispatchRecord
            )
            if binding is None or link is None:
                return "no recorded dispatch of this invocation in this run scope"
            outcome = self._sessions.inspect(
                AgentSessionKey.parse(binding.session_key), invocation.invocation_id.root
            )
        except (
            ReceiptCorruptError,
            SessionPersistenceError,
            SessionConfigurationError,
            InvocationConflictError,
        ) as error:
            return f"invocation evidence is unreadable: {error}"
        if isinstance(outcome, (Completed, InvalidResponse)):
            return None
        return "the invocation is not proven terminal, so its writer may still run"


__all__ = [
    "DispatchRecord",
    "JournalRunInvocations",
    "RuntimeSessionRequests",
    "SessionBinding",
    "SessionResolver",
]
