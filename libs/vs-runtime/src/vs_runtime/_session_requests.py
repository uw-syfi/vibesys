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
    Scope,
    SessionId,
    SessionInput,
    SessionObserved,
    SessionSpec,
    SetupFailureKind,
    SnapshotAndRetainRun,
    TargetObservation,
    TurnObserved,
    TurnSpec,
    WorkspaceRef,
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
from vs_runtime._workspace_access import AccessGrant, enforce_workspace_access
from vs_runtime.contracts import RuntimeContractError

if TYPE_CHECKING:
    from datetime import timedelta

    from vs_agent.api import AgentSessions, AgentSessionSpec, ClientAgentSessions, InvocationOutcome
    from vs_core.api import InvocationId, Observation, RequestBase
    from vs_prompts.api import RenderedPrompt
    from vs_runtime._core_requests import OwnerEvent, SessionRoleRequest
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._workspace_access import AccessGuardedWorkspace

_BINDINGS = "session-bindings"
_DISPATCHES = "session-dispatches"
_ACCESS = "session-access"


def session_binding_key(request: RequestBase, session_id: SessionId) -> str:
    """The one key under which a session's binding and dispatches are stored."""
    return f"{owner_key(request)}/{session_id.root}"


def load_session_binding(store: ReceiptStore, bkey: str) -> SessionBinding | None:
    """Read a binding; the only reader of the binding family outside this module's writers."""
    return store.load(_BINDINGS, "binding", bkey, SessionBinding)


def conversation_established(
    sessions: AgentSessions, binding: SessionBinding, *, excluding: str | None = None
) -> bool:
    """Whether the journal shows a completed turn, so the provider conversation exists."""
    key = AgentSessionKey.parse(binding.session_key)
    others = (i for i in reversed(binding.dispatched) if i != excluding)
    return any(isinstance(sessions.inspect(key, i), Completed) for i in others)


def load_dispatch_record(
    store: ReceiptStore, bkey: str, invocation_id: InvocationId
) -> DispatchRecord | None:
    """Read the dispatch record for one invocation of a bound session."""
    return store.load(_DISPATCHES, "dispatch", f"{bkey}/{invocation_id.root}", DispatchRecord)


class SessionResolver(Protocol):
    """What the executor needs from the run to turn core declarations into agent inputs.

    Every method answers ``None`` (or False) for something the run does not know;
    the executor then rejects the request and names the unknown item.
    """

    def knows_role(self, role: RoleId) -> bool:
        """Whether *role* is a declared agent role of this run."""
        ...

    async def workspace_for(self, ref: WorkspaceRef | Scope) -> AccessGuardedWorkspace | None:
        """The live workspace a turn runs in, or None when it no longer exists."""
        ...

    def access_grant(self, turn: TurnSpec) -> AccessGrant | None:
        """What the turn's role may write; the executor reverts and reports anything else."""
        ...

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The provider session configuration for *turn* (role, workspace, policy)."""
        ...

    def output_schema(self, ref: SchemaRef) -> type[BaseModel] | None:
        """The pydantic type that validates replies for *ref*."""
        ...

    def turn_timeout(self, role: RoleId) -> timedelta | None:
        """The role's in-turn timeout: one constant per role, None for the driver default.

        The executor records it when the session is bound and rejects a turn when the
        resolver later answers differently, because the durable session fences it.
        """
        ...

    def template(self, turn: TurnSpec) -> AgentTurnRequest | None:
        """The fixed per-role turn configuration: instructions and a constant label.

        These fields must not vary between turns of one conversation, because the
        durable session fences them. The executor adds message, schema, timeout and
        identity; a timeout set here is replaced by the bound one.
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
    turn_timeout_seconds: float | None = None
    """The role's in-turn timeout, fixed when the session was bound."""
    dispatched: tuple[str, ...] = ()
    """Invocations dispatched in this session, recorded before each provider call.

    The conversation is established once the journal shows one of them Completed,
    so no flag has to be written after a turn and no crash can skip the guard."""


class DispatchRecord(BaseModel):
    """Written before the provider is called: which request dispatches an invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: RequestId
    digest: str
    output_schema: SchemaRef


class AccessViolation(BaseModel):
    """A turn wrote outside its role's workspace access; the writes were reverted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    role_id: str
    paths: tuple[str, ...]


class AccessReceipt(BaseModel):
    """Written before the provider is called: how to undo what the turn may write.

    The baseline is the workspace snapshot taken before the turn. It outlives a host
    crash, so a replay or an inspection reverts the turn's unauthorized writes from
    the same baseline instead of taking the tainted tree as a new one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: WorkspaceRef | Scope
    grant: AccessGrant
    baseline: str
    violation: AccessViolation | None = None
    """Recorded before the revert starts, so a crash during it cannot lose the finding."""
    settled: bool = False
    """True once the workspace provably holds only what the grant allows."""


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


def _violated(violation: AccessViolation, resource_id: ResourceId) -> _Facts:
    """An accepted turn that wrote outside its access: its output is withheld."""
    return _Facts(
        ObservationStatus.FAILED,
        accepted=True,
        resource_id=resource_id,
        diagnostic=(
            f"role {violation.role_id} wrote outside its workspace access; "
            f"reverted: {', '.join(violation.paths)}"
        ),
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
    workspace: AccessGuardedWorkspace
    grant: AccessGrant


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
            return await self._inspect(request, context)
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
        return session_binding_key(request, session_id)

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
        timeout = self._resolver.turn_timeout(spec.role_id)
        if timeout is not None and timeout.total_seconds() <= 0:
            return _rejected(f"role {spec.role_id.root} declares an invalid turn timeout")
        wanted = SessionBinding(
            spec=spec,
            ensure_request=request_id,
            resource_id=ResourceId(root=f"session:{key}"),
            session_key=self._conversation_key(request, spec),
            turn_timeout_seconds=None if timeout is None else timeout.total_seconds(),
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
        if await self._established(key, excluding=None):
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

    async def _resolve(self, request: DispatchTurn, bkey: str) -> tuple[SessionBinding, _Dispatch]:
        """The ensured session and the resolved dispatch, or a refusal naming what is missing."""
        turn = request.turn
        binding = load_session_binding(self._store, bkey)
        if binding is None or not _same_session(binding.spec, turn.session):
            raise _RefusalError(_rejected("no ensured session matches this turn's session"))
        schema = self._resolver.output_schema(turn.output_schema)
        if schema is None:
            raise _RefusalError(
                _rejected(f"output schema {turn.output_schema.name} is not registered")
            )
        workspace = await self._resolver.workspace_for(turn.workspace)
        grant = self._resolver.access_grant(turn)
        if workspace is None or grant is None:
            raise _RefusalError(_rejected("the turn's workspace or access cannot be resolved"))
        spec = self._resolver.agent_spec(turn, workspace)
        template = self._resolver.template(turn)
        message = self._resolver.message(turn, request.inputs)
        if spec is None or template is None or message is None:
            raise _RefusalError(
                _rejected("the turn's session, prompts or inputs cannot be resolved")
            )
        timeout = self._resolver.turn_timeout(turn.session.role_id)
        if (None if timeout is None else timeout.total_seconds()) != binding.turn_timeout_seconds:
            raise _RefusalError(
                _rejected(
                    f"role {turn.session.role_id.root} changed its turn timeout since the "
                    "session was bound",
                    binding.resource_id,
                )
            )
        dispatch = _Dispatch(
            AgentSessionKey.parse(binding.session_key),
            spec,
            replace(
                template,
                output_schema=schema,
                timeout=None if timeout is None else timeout,
            ),
            message,
            turn.invocation_id.root,
            schema,
            workspace,
            grant,
        )
        return binding, dispatch

    async def _dispatch(self, request: DispatchTurn, call: _Call) -> _Facts:
        turn = request.turn
        bkey = self._binding_key(request, turn.session.session_id)
        binding, dispatch = await self._resolve(request, bkey)
        ended = await self._admit(request, call, bkey, binding, dispatch)
        if ended is not None:
            return ended
        await self._begin_access(request, bkey, dispatch)
        try:
            outcome = await self._outcome(bkey, binding, dispatch)
        except InvocationConflictError as error:
            return _rejected(f"invocation conflicts with its journal: {error}", binding.resource_id)
        except SessionConfigurationError as error:
            return _rejected(f"session configuration refused: {error}", binding.resource_id)
        except (SessionPersistenceError, SessionResumeError) as error:
            return _unknown(f"dispatch state is unreadable: {error}", binding.resource_id)
        return await self._finish(outcome, bkey, binding, dispatch, turn.output_schema)

    async def _outcome(
        self, bkey: str, binding: SessionBinding, dispatch: _Dispatch
    ) -> InvocationOutcome:
        """The journal's outcome for the invocation, dispatching it only if it never ended.

        A turn that already ended is read back: dispatching it through the other of
        start and resume would look like a changed payload to the journal.
        """
        recorded = await asyncio.to_thread(
            self._sessions.inspect, dispatch.key, dispatch.invocation
        )
        if isinstance(recorded, (Completed, InvalidResponse)):
            return recorded
        established = await self._established(bkey, excluding=dispatch.invocation)
        if established:
            await self._require_checkpoint(binding)
        return await await_session_operation(
            asyncio.create_task(
                asyncio.to_thread(self._start_or_resume, dispatch, established=established)
            )
        )

    async def _established(self, bkey: str, *, excluding: str | None) -> bool:
        """Whether the journal shows a completed turn, so the provider conversation exists."""
        binding = load_session_binding(self._store, bkey)
        if binding is None:
            return False
        return await asyncio.to_thread(
            conversation_established, self._sessions, binding, excluding=excluding
        )

    def _note_dispatch(self, bkey: str, invocation: str) -> None:
        """Record, in the binding and before the provider call, that this invocation may run."""

        def append(stored: SessionBinding | None) -> tuple[SessionBinding | None, None]:
            if stored is None or invocation in stored.dispatched:
                return None, None
            return stored.model_copy(update={"dispatched": (*stored.dispatched, invocation)}), None

        self._store.modify(_BINDINGS, "binding", bkey, SessionBinding, append)

    async def _finish(
        self,
        outcome: InvocationOutcome,
        bkey: str,
        binding: SessionBinding,
        dispatch: _Dispatch,
        ref: SchemaRef,
    ) -> _Facts:
        """What a journal outcome means: judge the turn against its workspace access.

        A turn that ended is judged against its workspace access before anyone sees
        its output; one that may still be running is not, because it may still write.
        """
        facts = self._translate(outcome, binding, dispatch.schema, ref)
        if not isinstance(outcome, (Completed, InvalidResponse)):
            return facts
        violation = await self._settle_access(f"{bkey}/{dispatch.invocation}")
        return facts if violation is None else _violated(violation, binding.resource_id)

    async def _begin_access(self, request: DispatchTurn, bkey: str, dispatch: _Dispatch) -> None:
        """Snapshot the workspace once, durably, before the turn can write to it."""
        key = f"{bkey}/{dispatch.invocation}"
        if self._store.load(_ACCESS, "access", key, AccessReceipt) is not None:
            return  # a replay keeps the first baseline, never the tree the turn left
        baseline = await await_session_operation(
            asyncio.create_task(dispatch.workspace.snapshot(f"{key}-input"))
        )
        receipt = AccessReceipt(
            workspace=request.turn.workspace, grant=dispatch.grant, baseline=baseline
        )

        def keep_first(stored: AccessReceipt | None) -> tuple[AccessReceipt | None, None]:
            return (receipt if stored is None else None), None

        self._store.modify(_ACCESS, "access", key, AccessReceipt, keep_first)

    async def _settle_access(self, key: str) -> AccessViolation | None:
        """Revert the turn's unauthorized writes from its recorded baseline; its violation, if any."""
        receipt = self._store.load(_ACCESS, "access", key, AccessReceipt)
        if receipt is None or receipt.settled:
            return None if receipt is None else receipt.violation
        workspace = await self._resolver.workspace_for(receipt.workspace)
        if workspace is None:
            raise _RefusalError(_unknown("the turn's workspace is gone before its access settled"))

        def record(paths: list[str]) -> None:
            found = AccessViolation(role_id=receipt.grant.role_id, paths=tuple(paths))
            self._store.replace(
                _ACCESS, "access", key, receipt.model_copy(update={"violation": found})
            )

        try:
            await await_session_operation(
                asyncio.create_task(
                    enforce_workspace_access(
                        workspace, receipt.grant, receipt.baseline, observer=record
                    )
                )
            )
        except RuntimeContractError as error:
            raise _RefusalError(_unknown(f"workspace access is not restored: {error}")) from error
        latest = self._store.load(_ACCESS, "access", key, AccessReceipt) or receipt
        self._store.replace(_ACCESS, "access", key, latest.model_copy(update={"settled": True}))
        return latest.violation

    async def _admit(
        self,
        request: DispatchTurn,
        call: _Call,
        bkey: str,
        binding: SessionBinding,
        dispatch: _Dispatch,
    ) -> _Facts | None:
        """Record the dispatch before any provider call; facts when the request ends here."""
        turn = request.turn
        link = DispatchRecord(
            request_id=call.request_id,
            digest=call.context.payload_digest,
            output_schema=turn.output_schema,
        )
        record_key = f"{bkey}/{dispatch.invocation}"
        recorded = self._store.load(_DISPATCHES, "dispatch", record_key, DispatchRecord)
        if recorded is not None and recorded != link:
            return _rejected("invocation is dispatched by another request", binding.resource_id)
        if turn.deadline_at <= call.context.now_at:
            if recorded is None:
                # No dispatch record proves no provider call began.
                return _rejected("the turn deadline passed before dispatch", binding.resource_id)
            # A recorded dispatch may have started: replay what the journal proves, never reject.
            return await self._after_deadline(bkey, binding, dispatch, turn.output_schema)
        # Written before any provider call: its absence later proves no dispatch began.
        self._store.record_once(_DISPATCHES, "dispatch", record_key, link)
        self._note_dispatch(bkey, dispatch.invocation)
        return None

    async def _after_deadline(
        self, bkey: str, binding: SessionBinding, dispatch: _Dispatch, ref: SchemaRef
    ) -> _Facts:
        """The recorded outcome of a dispatch whose deadline passed; Unknown unless settled."""
        try:
            outcome = self._sessions.inspect(dispatch.key, dispatch.invocation)
        except (
            SessionPersistenceError,
            SessionConfigurationError,
            InvocationConflictError,
        ) as error:
            return _unknown(str(error), binding.resource_id)
        facts = await self._finish(outcome, bkey, binding, dispatch, ref)
        if facts.terminal:
            return facts
        return _unknown(
            f"the turn deadline passed and the dispatch may have started: {facts.diagnostic}",
            binding.resource_id,
        )

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

    async def _inspect(self, request: InspectTurn, context: ExecutionContext) -> ExecutionOutcome:
        """Translate the correlated durable facts of one invocation; nothing is dispatched."""
        try:
            return await self._inspect_facts(request, context)
        except _RefusalError as refusal:
            return self._result(request, context, refusal.facts)
        except ReceiptCorruptError as error:
            return self._result(request, context, _unknown(str(error)))

    async def _inspect_facts(
        self, request: InspectTurn, context: ExecutionContext
    ) -> ExecutionOutcome:
        invocation = request.invocation
        bkey = self._binding_key(request, invocation.session_id)
        binding = load_session_binding(self._store, bkey)
        if binding is None:
            raise _RefusalError(_rejected("no ensured session for this invocation"))
        link = load_dispatch_record(self._store, bkey, invocation.invocation_id)
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
        if isinstance(outcome, (Completed, InvalidResponse)):
            violation = await self._settle_access(f"{bkey}/{invocation.invocation_id.root}")
            if violation is not None:
                target = _violated(violation, binding.resource_id)
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
                ),
            ),
            owner_events=(self._turn_event(request.invocation, seen, target),),
        )


class JournalRunInvocations:
    """Run-invocation proof from the session journal: only a settled turn has no live writer.

    Completed and schema-rejected turns ended once their workspace access was
    enforced. Never-dispatched, in-flight and Unknown turns prove nothing, because the
    provider may still be writing; neither does a turn whose unauthorized writes are
    not yet reverted, because a snapshot would retain them.
    """

    def __init__(self, sessions: ClientAgentSessions, store: ReceiptStore) -> None:
        """Read the same journal and bindings the session executor writes."""
        self._sessions = sessions
        self._store = store

    def unproven(self, request: SnapshotAndRetainRun) -> str | None:
        """None when the invocation's turn settled; otherwise why its writer is not proven gone."""
        invocation = request.invocation
        bkey = session_binding_key(request, invocation.session_id)
        try:
            binding = load_session_binding(self._store, bkey)
            link = load_dispatch_record(self._store, bkey, invocation.invocation_id)
            if binding is None or link is None:
                return "no recorded dispatch of this invocation in this run scope"
            access = self._store.load(
                _ACCESS, "access", f"{bkey}/{invocation.invocation_id.root}", AccessReceipt
            )
            if access is not None and not access.settled:
                return "the turn's workspace access has not been enforced yet"
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
    "AccessReceipt",
    "AccessViolation",
    "DispatchRecord",
    "JournalRunInvocations",
    "RuntimeSessionRequests",
    "SessionBinding",
    "SessionResolver",
]
