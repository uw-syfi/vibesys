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

from pydantic import BaseModel, ConfigDict, field_validator

from vs_agent.api import (
    AgentExecutionCancelledError,
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentTurnRequest,
    Completed,
    InvalidResponse,
    InvocationConflictError,
    InvocationOutcome,
    Pending,
    SchemaViolation,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
    SteerOutcome,
    Unknown,
    parse_typed_response,
)
from vs_core.api import (
    REPLY_ERROR_MESSAGE_LIMIT,
    DispatchTurn,
    EnsureSession,
    InputId,
    InputMidTurnReleased,
    InspectTurn,
    InvocationRef,
    ObservationStatus,
    ReissueProof,
    ReplyError,
    ReplyErrorCode,
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
    TurnPermit,
    TurnSpec,
    WorkspaceRef,
)
from vs_runtime._access_settlement import (
    AccessKey,
    AccessReceipt,
    AccessSettlement,
    AccessSettlementError,
    AccessUnrestorableError,
    AccessViolation,
    access_unproven,
)
from vs_runtime._core_identity import CoreRequestId, ReceiptKey, core_identity, core_named
from vs_runtime._core_requests import ExecutionContext, ExecutionOutcome, ExecutionResult
from vs_runtime._effect_fence import EffectFences, Entered, Fenced, FenceKey, FenceKind
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
from vs_runtime._session_operation import await_session_operation

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import timedelta
    from pathlib import Path

    from vs_agent.api import AgentSessions, AgentSessionSpec, ClientAgentSessions, InvocationOutcome
    from vs_core.api import (
        Continuation,
        DecisionId,
        InvocationId,
        MidTurnOffer,
        Observation,
        RequestBase,
    )
    from vs_prompts.api import RenderedPrompt
    from vs_runtime._core_requests import OwnerEvent, SessionRoleRequest
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._workspace_access import AccessGrant, AccessGuardedWorkspace
    from vs_sim.api import BlockingRunner

_BINDINGS = "session-bindings"
_DISPATCHES = "session-dispatches"


def session_binding_key(request: RequestBase, session_id: SessionId) -> str:
    """The one key under which a session's binding and dispatches are stored."""
    return scope_binding_key(request.scope, session_id)


def scope_binding_key(scope: Scope, session_id: SessionId) -> str:
    """``session_binding_key`` for a session of ``scope``, without a request in hand."""
    return f"{scope.owner.kind}:{scope.owner.root}:{scope.generation}/{session_id.root}"


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


def invocation_fence(bkey: str, invocation_id: str) -> FenceKey:
    """The fence of one invocation's provider turn: DispatchTurn enters it, CancelTurn closes it."""
    return FenceKey(FenceKind.INVOCATION, f"{bkey}/{invocation_id}")


def episode_fence(bkey: str, episode: DecisionId | None) -> FenceKey:
    """The fence of one admission episode of a session: EnsureSession enters it, CloseSession closes it.

    A later episode reattaches the same session under the same binding key, so the fence
    names the episode the close ended, never the session alone.
    """
    return FenceKey(
        FenceKind.SESSION_EPISODE,
        f"{bkey}/run" if episode is None else f"{bkey}/episode:{episode.root}",
    )


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

    def guard_snapshots(self, fenced_by: Callable[[Path], tuple[str, ...]]) -> None:
        """Make every snapshot of every workspace of this run ask *fenced_by* first.

        *fenced_by* takes a workspace's host path and names the invocations whose writes
        are not judged; a snapshot is refused while it names any. Called once, when the
        executors are built, so the fence is in place before anything can snapshot.
        """
        ...

    def access_grant(self, turn: TurnSpec) -> AccessGrant | None:
        """What the turn's role may write; the executor reverts and reports anything else."""
        ...

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The provider session configuration for *turn* (role, workspace, policy)."""
        ...

    def hold_permit(self, execution_id: str, scope: Scope, permit: TurnPermit) -> None:
        """Record the logical-turn permit before preparation publishes the execution.

        A resolver with no control channel records nothing.
        """
        ...

    def output_schema(self, ref: SchemaRef) -> type[BaseModel] | None:
        """The pydantic type that validates replies for *ref*."""
        ...

    def turn_timeout(self, role: RoleId) -> timedelta | None:
        """The role's in-turn timeout: one constant per role, None for the client default.

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

    def mid_turn_message(self, item: SessionInput) -> RenderedPrompt | None:
        """The message offering ``item`` to a turn already running; ``None`` as ``message``."""
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


LEGACY_DERIVED_SUFFIX = ".dispatch"
"""What a resume's nested step id ended in, before records named the resume request."""


class DispatchRecord(BaseModel):
    """Written before the provider is called: which request dispatches an invocation.

    The request is the one core knows as the dispatcher: a resumed turn is dispatched on
    behalf of its ``ResumeSessionTurn``, so that request's identity is what is recorded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: CoreRequestId
    digest: str
    output_schema: SchemaRef

    @field_validator("request_id", mode="before")
    @classmethod
    def _core_request(cls, value: object) -> object:
        """Read a record written before resumes recorded core's id as the id core knows.

        A resume once recorded its nested step's derived ``<R>.dispatch`` id, which core
        cannot look up. Only that derived suffix is mapped (to ``R``), so no record
        read back can name an id core never sent.
        """
        stored = value.model_dump() if isinstance(value, RequestId) else value
        if isinstance(stored, dict) and str(stored.get("root", "")).endswith(LEGACY_DERIVED_SUFFIX):
            root = str(stored["root"]).removesuffix(LEGACY_DERIVED_SUFFIX)
            return RequestId.model_validate({**stored, "root": root})
        return value


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
    reply_errors: tuple[ReplyError, ...] = ()
    settles: bool = True
    """False when the answer proves nothing about what the request holds (a reused identity),
    so even a terminal one claims no release and no complete children."""
    refused_steers: tuple[str, ...] = ()
    """Interrupt-mode inputs the provider took into the turn and did not keep, by input id."""


def _refusals(
    invocation: InvocationRef, facts: _Facts, now_at: float
) -> tuple[InputMidTurnReleased, ...]:
    """The interrupt-mode inputs the turn's provider refused, released ahead of its acceptance.

    They ride in the same result as the turn's end, so the refusal commits with it and core
    gives each input to the next turn instead of delivering it with this one.
    """
    return tuple(
        InputMidTurnReleased(
            input_id=InputId(root=steer_id), invocation=invocation, now_at=now_at, refused=True
        )
        for steer_id in facts.refused_steers
    )


def _reply_errors(violations: tuple[SchemaViolation, ...]) -> tuple[ReplyError, ...]:
    """What a validator reported about a rejected reply, as core's typed errors."""
    return tuple(
        ReplyError(
            path=item.path,
            code=ReplyErrorCode.INVALID_VALUE,
            message=item.message[:REPLY_ERROR_MESSAGE_LIMIT],
        )
        for item in violations
    )


def _same_session(bound: SessionSpec, asked: SessionSpec) -> bool:
    """Same session identity, role, lifetime and access; the policy only says how to acquire it."""
    return bound == asked.model_copy(update={"policy": bound.policy})


def _conflict() -> _Facts:
    """The refusal of a reused identity: it proves nothing about what the identity holds."""
    facts = ObservationFacts.payload_conflict()
    return _Facts(
        facts.status,
        terminal=facts.terminal,
        diagnostic=facts.diagnostic,
        setup_failure=SetupFailureKind.PERMANENT,
        settles=False,
    )


def _rejected(diagnostic: str, resource_id: ResourceId | None = None) -> _Facts:
    return _Facts(
        ObservationStatus.REJECTED,
        resource_id=resource_id,
        diagnostic=diagnostic,
        setup_failure=SetupFailureKind.PERMANENT,
    )


def _retryable(diagnostic: str, resource_id: ResourceId | None = None) -> _Facts:
    """The executor did not run the request, and a later identical one may: never stored."""
    return _Facts(
        ObservationStatus.FAILED, terminal=False, resource_id=resource_id, diagnostic=diagnostic
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


def _unrestorable(error: AccessUnrestorableError, resource_id: ResourceId) -> _Facts:
    """An ended turn whose writes outside its access could not be undone: it fails for good.

    Retrying the revert would leave the same paths, so the turn is not left unknown
    until its limit; its output is withheld and the reason is stated.
    """
    return _Facts(
        ObservationStatus.FAILED, accepted=True, resource_id=resource_id, diagnostic=str(error)
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
    standalone: bool
    """The message is the role's own prompt, so the turn may run in a fresh conversation."""


def _stands_alone(turn: TurnSpec) -> bool:
    """Whether the turn's message carries the role's whole prompt rather than answering the last.

    A paid or free turn asks the role's own prompt. A correction answers the reply before it
    and a resume answers the wait the conversation recorded, so both need that conversation.
    """
    return turn.charge_class in ("paid", "free") and turn.predecessor is None


_WITHDRAWN = "the invocation was withdrawn before dispatch"


class _WithdrawnError(Exception):
    """A cancel closed the invocation's fence before the provider call began."""


class _RefusalError(Exception):
    """A validation step ended the request with these terminal or unknown facts."""

    def __init__(self, facts: _Facts) -> None:
        super().__init__(facts.diagnostic)
        self.facts = facts


@dataclass(frozen=True)
class _Call:
    """One request with the identity core guarantees it carries."""

    request: EnsureSession | DispatchTurn
    receipt_key: ReceiptKey
    """Where its receipt is stored: a resume's nested step has a key of its own."""
    canonical: CoreRequestId
    """The request core knows as the one executing: what the dispatch record names."""
    context: ExecutionContext
    mark: str | None = None
    """What the turn's agent processes are marked with, when a step runner asked for the turn."""


class TurnYields[TurnT, WaitT](Protocol):
    """Where a turn that asked to wait gets its continuation.

    Asked once when a dispatched turn ends. The positional core binds a
    ``DispatchTurn`` to a ``Continuation`` that core validates and publishes after
    the checkpoint. The native replay log binds a physical turn's span identity to
    its durable ``ReplayTurnWait``, which the archive checks against the log.
    """

    def yielded(self, request: TurnT, /) -> WaitT | None:
        """The continuation this turn yielded, or None when it did not wait."""
        ...


class RuntimeSessionRequests:
    """Translate session requests into durable ``ClientAgentSessions`` calls, once each."""

    def __init__(
        self,
        sessions: ClientAgentSessions,
        resolver: SessionResolver,
        store: ReceiptStore,
        yields: TurnYields[DispatchTurn, Continuation] | None = None,
        *,
        blocking: BlockingRunner,
    ) -> None:
        """Bind the durable agent sessions and the run's resolver to the shared store.

        *blocking* runs the journal and provider calls off the event loop.
        """
        self._sessions = sessions
        self._resolver = resolver
        self._store = store
        self._fences = EffectFences(store)
        self._yields = yields
        self._blocking = blocking
        self.settlement = AccessSettlement(store, resolver.workspace_for)
        resolver.guard_snapshots(self.settlement.fenced_by)
        """Judges each invocation's writes; the lifecycle executors share it."""
        self._observations = ObservationFactory(store)
        self._locks: dict[str, asyncio.Lock] = {}

    def offer_mid_turn(self, offer: MidTurnOffer) -> SteerOutcome:
        """Offer an interrupt-mode input to the running turn ``offer`` names.

        Only this instance can reach a turn it runs. A session it never bound, an unreadable
        binding or input text, or a journal that disowns the invocation leaves no turn here
        that could take the message: ``NO_RUNNING_TURN``, so core gives it to the next turn.
        A ``DELIVERED`` message the provider then refuses comes back with the turn's result,
        which releases it before the turn's acceptance commits.
        """
        invocation = offer.invocation
        try:
            binding = load_session_binding(
                self._store, scope_binding_key(offer.scope, invocation.session_id)
            )
        except ReceiptCorruptError:
            return SteerOutcome.NO_RUNNING_TURN
        message = self._resolver.mid_turn_message(offer.input)
        if binding is None or message is None:
            return SteerOutcome.NO_RUNNING_TURN
        try:
            return self._sessions.steer(
                AgentSessionKey.parse(binding.session_key),
                invocation.invocation_id.root,
                message,
                steer_id=offer.input.input_id.root,
            )
        except InvocationConflictError:
            return SteerOutcome.NO_RUNNING_TURN

    async def execute(
        self, request: SessionRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome:
        """Execute, replay or inspect one request under its canonical identity."""
        canonical = core_identity(request)
        if isinstance(request, InspectTurn):
            return await self._inspect(request, context)
        if not isinstance(request, (EnsureSession, DispatchTurn)):
            return self._result(request, context, _rejected(f"{request.kind} is not executed here"))
        return await self._locked(_Call(request, ReceiptKey(canonical.root), canonical, context))

    async def dispatch_on_behalf_of(
        self, request: DispatchTurn, behalf: CoreRequestId, context: ExecutionContext
    ) -> ExecutionOutcome:
        """Dispatch a turn core asked for through another request, such as a resume.

        *request* is this executor's own step: its identity keys the receipt and the
        observations. The dispatch record names *behalf*, the request core knows, so an
        inspection of the turn answers with an identity core can look up.
        """
        if request.request_id is None:
            message = "request_id: execution requires a canonical identity"
            raise ValueError(message)
        return await self._locked(
            _Call(request, ReceiptKey(request.request_id.root), behalf, context)
        )

    async def _locked(self, call: _Call) -> ExecutionOutcome:
        request = call.request
        lock_key = f"{owner_key(request)}/{self._session_id(request).root}"
        async with self._locks.setdefault(lock_key, asyncio.Lock()):
            return await self._run(call)

    async def attempt(
        self,
        request: DispatchTurn,
        context: ExecutionContext,
        *,
        mark: str,
        behalf: CoreRequestId | None = None,
    ) -> ExecutionResult:
        """Run one attempt of a turn and return what it observed, recording no receipt of it.

        The caller owns the step's result. What survives an attempt that dies is the invocation
        journal, which replays an answered invocation instead of asking it again, and the access
        receipt, whose baseline the next attempt is judged against. A turn core asked for as
        another kind of request (a resume) names that request in *behalf*, which its dispatch
        record names. Every agent process the turn starts carries *mark*, which the step's
        runner finds them by after the process that started the turn died.
        """
        canonical = behalf or core_identity(request)
        call = _Call(request, ReceiptKey(canonical.root), canonical, context, mark)
        lock_key = f"{owner_key(request)}/{self._session_id(request).root}"
        async with self._locks.setdefault(lock_key, asyncio.Lock()):
            return (await self._performed(call)).result

    async def _performed(
        self, call: _Call
    ) -> Settled[ExecutionResult] | Transient[ExecutionResult]:
        try:
            facts = await self._perform(call)
        except _RefusalError as refusal:
            facts = refusal.facts
        except ReceiptCorruptError as error:
            facts = _unknown(str(error))
        result = self._result(call.request, call.context, facts)
        return Settled(result) if result.observation.observation.terminal else Transient(result)

    async def _run(self, call: _Call) -> ExecutionOutcome:
        request, context = call.request, call.context

        async def perform(
            *, resumed: bool
        ) -> Settled[ExecutionResult] | Transient[ExecutionResult]:
            del resumed  # the journal, not this flag, says whether a dispatch happened
            return await self._performed(call)

        execution = await self._store.run_once(
            call.receipt_key,
            owner=owner_key(request),
            context=context,
            result_type=ExecutionResult,
            perform=perform,
        )
        match execution:
            case Replayed(result) | Performed(result):
                return result
            case Conflict():
                return self._result(request, context, _conflict())
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
        released = facts.settles and isinstance(request, DispatchTurn) and facts.terminal
        if released and not facts.accepted and facts.resource_id is None:
            # A turn rejected without naming its session was refused while resolving it, before
            # any dispatch record; the refusal is sealed, so the turn never dispatches.
            observed = ObservationFacts.refused(facts.diagnostic)
        else:
            observed = ObservationFacts(
                status=facts.status,
                terminal=facts.terminal,
                accepted=facts.accepted,
                released=released,
                children_complete=facts.settles and facts.terminal,
                resource_id=facts.resource_id,
                diagnostic=facts.diagnostic,
            )
        observation = self._observations.observe(
            ObservationSubject.of(request), observed, observed_at=context.now_at
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
            suspension = (
                self._yields.yielded(request)
                if self._yields is not None
                and facts.accepted
                and facts.terminal
                and facts.status == ObservationStatus.SUCCEEDED
                else None
            )
            events = (
                *_refusals(invocation, facts, context.now_at),
                self._turn_event(invocation, observation, facts, suspension),
            )
        return ExecutionResult(
            observation=RequestObserved(
                observation=observation,
                setup_failure=facts.setup_failure,
                reply_errors=facts.reply_errors,
            ),
            owner_events=events,
        )

    @staticmethod
    def _turn_event(
        invocation: InvocationRef,
        observation: Observation,
        facts: _Facts,
        suspension: Continuation | None = None,
    ) -> TurnObserved:
        return TurnObserved(
            suspension=suspension,
            invocation=invocation,
            observation=observation,
            output_schema=facts.output_schema,
            output_json=facts.output_json,
            reply_errors=facts.reply_errors,
        )

    # EnsureSession and DispatchTurn

    async def _perform(self, call: _Call) -> _Facts:
        if isinstance(call.request, EnsureSession):
            return await self._ensure(call.request, call)
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

    async def _ensure(self, request: EnsureSession, call: _Call) -> _Facts:
        request_id = call.canonical
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
        binding = self._bind(key, wanted, required, episode_fence(key, request.admission_id), call)
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
        self,
        key: str,
        wanted: SessionBinding,
        required: ResourceId | None,
        fence: FenceKey,
        call: _Call,
    ) -> SessionBinding:
        """The session's binding: created for a fresh identity, never for a required reattach.

        The binding is written in the same locked step as the ensure enters its episode's
        fence, so a close of the episode either comes first and no binding is ever made for
        it, or comes after and finds the binding to release.
        """
        if required is not None and wanted.spec.policy != "reuse":
            raise _RefusalError(_rejected("a required resource needs the reuse policy", required))
        with self._store.exclusive():
            stored = load_session_binding(self._store, key)
            if stored is None and required is not None:
                raise _RefusalError(
                    _rejected(
                        "no durable correspondence for the required session resource; "
                        "a fresh conversation will not replace it",
                        required,
                    )
                )
            admission = self._fences.enter(fence, by=call.canonical, host=call.context.fence)
            if isinstance(admission, Fenced) and not (
                admission.entered_before and stored is not None
            ):
                raise _RefusalError(_rejected("the session was closed before this ensure bound it"))
            if stored is None:
                self._store.replace(_BINDINGS, "binding", key, wanted)
                stored = wanted
            self._fences.leave(fence, by=call.canonical)
            return stored

    async def _require_checkpoint(self, binding: SessionBinding) -> None:
        """Raise unless the provider conversation of an established session is present."""
        key = AgentSessionKey.parse(binding.session_key)
        try:
            await self._blocking.run(self._sessions.checkpoint, key)
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

    async def _resolve(
        self, request: DispatchTurn, bkey: str, call: _Call
    ) -> tuple[SessionBinding, _Dispatch]:
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
        scope = turn.workspace if isinstance(turn.workspace, Scope) else turn.workspace.scope
        if call.context.permit is not None:
            self._resolver.hold_permit(turn.invocation_id.root, scope, call.context.permit)
        spec = self._resolver.agent_spec(turn, workspace)
        template = self._resolver.template(turn)
        message = self._resolver.message(turn, (*request.inputs, *request.guidance))
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
                step_tag=call.mark,
            ),
            message,
            turn.invocation_id.root,
            schema,
            workspace,
            grant,
            _stands_alone(turn),
        )
        return binding, dispatch

    async def _dispatch(self, request: DispatchTurn, call: _Call) -> _Facts:
        turn = request.turn
        bkey = self._binding_key(request, turn.session.session_id)
        binding, dispatch = await self._resolve(request, bkey, call)
        access_key = AccessKey(binding=bkey, invocation=dispatch.invocation)
        if self.settlement.receipt(access_key) is None:
            # Before the dispatch is recorded: a refusal here proves no provider call began.
            try:
                await self._settle_ended_peers(dispatch.workspace.path, access_key)
                self.settlement.require_clear(dispatch.workspace.path, access_key)
            except AccessUnrestorableError as error:
                # An earlier turn's writes cannot be undone: no turn may baseline this
                # workspace, now or on a retry, so refuse for good and say why.
                return _rejected(str(error), binding.resource_id)
            except AccessSettlementError as error:
                return _retryable(str(error), binding.resource_id)
        fence = invocation_fence(bkey, dispatch.invocation)
        ended = await self._admit(request, call, bkey, binding, dispatch)
        if ended is not None:
            return ended
        try:
            return await self._run_turn(request, bkey, binding, dispatch, call)
        finally:
            # Whatever this delivery started is in the journal now: a cancel may judge it.
            self._fences.leave(fence, by=call.canonical)

    async def _settle_ended_peers(self, workspace: Path, key: AccessKey) -> None:
        """Judge the unsettled turns of this workspace whose journal proves they ended.

        A host that died between a turn's journal write and its settlement left
        them; a new baseline must not adopt their writes. Turns that may still run
        are left, and ``require_clear`` then refuses.
        """
        for peer in self.settlement.blocking(workspace, key):
            binding = load_session_binding(self._store, peer.binding)
            if binding is None:
                continue
            try:
                outcome, never_journaled = await self._blocking.run(
                    self._inspect_peer, AgentSessionKey.parse(binding.session_key), peer.invocation
                )
            except (
                SessionPersistenceError,
                SessionConfigurationError,
                InvocationConflictError,
            ) as error:
                message = f"invocation evidence is unreadable: {error}"
                raise AccessSettlementError(message) from error
            if isinstance(outcome, (Completed, InvalidResponse)) or (
                isinstance(outcome, Unknown) and never_journaled
            ):
                await self.settlement.settle(peer)

    def _inspect_peer(
        self, key: AgentSessionKey, invocation_id: str
    ) -> tuple[InvocationOutcome, bool]:
        """The invocation's evidence and whether it has no journal row (one blocking read)."""
        return self._sessions.inspect(key, invocation_id), self._never_journaled(invocation_id)

    def _never_journaled(self, invocation_id: str) -> bool:
        """Whether the invocation has no journal row.

        Dispatch intent is journaled before any provider call, so a turn with no row never
        began (a refused resume, or a host that died before it dispatched): it wrote nothing.
        """
        with self._sessions.invocation_transaction() as slot:
            state = slot.load_optional()
        return state is None or invocation_id not in state.invocations

    async def _run_turn(
        self,
        request: DispatchTurn,
        bkey: str,
        binding: SessionBinding,
        dispatch: _Dispatch,
        call: _Call,
    ) -> _Facts:
        turn = request.turn
        access_key = AccessKey(binding=bkey, invocation=dispatch.invocation)
        try:
            await self.settlement.begin(
                access_key, turn.workspace, dispatch.workspace, dispatch.grant
            )
        except AccessUnrestorableError as error:
            # The workspace cannot be read by host Git, so no baseline exists and asking
            # again finds the same tree: refuse for good. No provider call began.
            return _rejected(str(error), binding.resource_id)
        except AccessSettlementError as error:
            # The baseline was refused, so no provider call began: ask again, do not inspect.
            return _retryable(str(error), binding.resource_id)
        try:
            outcome = await self._judged_outcome(access_key, bkey, binding, dispatch, call)
            if isinstance(outcome, _Facts):
                return outcome
            return await self._finish(outcome, bkey, binding, dispatch, turn.output_schema)
        finally:
            # ``begin`` acquired live ownership. Every later exit ends it here; an
            # unsettled durable receipt becomes the workspace fence, while successful
            # settlement has already removed that fence.
            self.settlement.fence(access_key)

    async def _judged_outcome(
        self,
        access_key: AccessKey,
        bkey: str,
        binding: SessionBinding,
        dispatch: _Dispatch,
        call: _Call,
    ) -> InvocationOutcome | _Facts:
        """The dispatch's journal outcome, or the facts of a dispatch that ended without one."""
        try:
            return await self._outcome(bkey, binding, dispatch, call)
        except (AgentExecutionCancelledError, _WithdrawnError) as cancelled:
            # Cancelled before the provider call: no turn ran, so it wrote nothing to judge.
            try:
                await self.settlement.settle(access_key)
            except AccessSettlementError as error:
                return _unknown(str(error), binding.resource_id)
            return (
                _Facts(
                    ObservationStatus.CANCELLED,
                    accepted=True,
                    resource_id=binding.resource_id,
                    diagnostic=str(cancelled),
                )
                if isinstance(cancelled, AgentExecutionCancelledError)
                else _rejected(_WITHDRAWN, binding.resource_id)
            )
        except InvocationConflictError as error:
            return _rejected(f"invocation conflicts with its journal: {error}", binding.resource_id)
        except SessionConfigurationError as error:
            return _rejected(f"session configuration refused: {error}", binding.resource_id)
        except (SessionPersistenceError, SessionResumeError) as error:
            return _unknown(f"dispatch state is unreadable: {error}", binding.resource_id)

    async def _outcome(
        self, bkey: str, binding: SessionBinding, dispatch: _Dispatch, call: _Call
    ) -> InvocationOutcome:
        """The journal's outcome for the invocation, dispatching it only if it never ended.

        A turn that already ended is read back: dispatching it through the other of
        start and resume would look like a changed payload to the journal. Just before the
        provider call the dispatch enters its fence again, so a cancel that closed it in
        the meantime stops the call (``_WithdrawnError``).
        """
        recorded = await self._blocking.run(
            self._sessions.inspect, dispatch.key, dispatch.invocation
        )
        if isinstance(recorded, (Completed, InvalidResponse)):
            return recorded
        established = await self._established(bkey, excluding=dispatch.invocation)
        if established:
            await self._require_checkpoint(binding)
        fence = invocation_fence(bkey, dispatch.invocation)
        if isinstance(
            self._fences.enter(fence, by=call.canonical, host=call.context.fence), Fenced
        ):
            if self._never_journaled(dispatch.invocation):
                raise _WithdrawnError
            return await self._blocking.run(
                self._sessions.inspect, dispatch.key, dispatch.invocation
            )
        return await await_session_operation(
            asyncio.create_task(
                self._blocking.run(self._start_or_resume, dispatch, established=established)
            ),
            stop=lambda: self._blocking.run(
                self._sessions.cancel, dispatch.key, dispatch.invocation
            ),
        )

    async def _established(self, bkey: str, *, excluding: str | None) -> bool:
        """Whether the journal shows a completed turn, so the provider conversation exists."""
        binding = load_session_binding(self._store, bkey)
        if binding is None:
            return False
        return await self._blocking.run(
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
        key = AccessKey(binding=bkey, invocation=dispatch.invocation)
        if not isinstance(outcome, (Completed, InvalidResponse)):
            return facts
        try:
            violation = await self.settlement.settle(key)
        except AccessUnrestorableError as error:
            return _unrestorable(error, binding.resource_id)
        except AccessSettlementError as error:
            raise _RefusalError(_unknown(str(error), binding.resource_id)) from error
        return facts if violation is None else _violated(violation, binding.resource_id)

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
            request_id=call.canonical,
            digest=call.context.payload_digest,
            output_schema=turn.output_schema,
        )
        record_key = f"{bkey}/{dispatch.invocation}"
        recorded = self._store.load(_DISPATCHES, "dispatch", record_key, DispatchRecord)
        if recorded is not None and recorded != link:
            return _rejected("invocation is dispatched by another request", binding.resource_id)
        if call.context.until.at <= call.context.now_at:
            if recorded is None:
                # No dispatch record proves no provider call began.
                return _rejected("the turn deadline passed before dispatch", binding.resource_id)
            # A recorded dispatch may have started: replay what the journal proves, never reject.
            return await self._after_deadline(bkey, binding, dispatch, turn.output_schema)
        # The dispatch enters the invocation's fence and records itself in one locked step,
        # before any provider call: a cancel either came first and no call ever begins, or
        # finds this record and judges the turn.
        fence = invocation_fence(bkey, dispatch.invocation)
        with self._store.exclusive():
            admission = self._fences.enter(fence, by=call.canonical, host=call.context.fence)
            if isinstance(admission, Entered):
                self._store.record_once(_DISPATCHES, "dispatch", record_key, link)
        if isinstance(admission, Fenced):
            return await self._after_cancel(admission, bkey, binding, dispatch, turn.output_schema)
        self._note_dispatch(bkey, dispatch.invocation)
        return None

    async def _after_cancel(
        self,
        admission: Fenced,
        bkey: str,
        binding: SessionBinding,
        dispatch: _Dispatch,
        ref: SchemaRef,
    ) -> _Facts:
        """A dispatch a cancel fenced off: report what the journal holds, start nothing."""
        if not admission.entered_before or self._never_journaled(dispatch.invocation):
            # No provider call began before the cancel, and none will.
            return _rejected(_WITHDRAWN, binding.resource_id)
        return await self._after_deadline(bkey, binding, dispatch, ref)

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
        return sessions.resume(key, dispatch.message, invocation, standalone=dispatch.standalone)

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
                        reply_errors=_reply_errors(error.violations),
                        refused_steers=outcome.result.refused_steers,
                    )
                return _Facts(
                    ObservationStatus.SUCCEEDED,
                    accepted=True,
                    resource_id=resource,
                    output_schema=ref,
                    output_json=reply.model_dump_json(),
                    refused_steers=outcome.result.refused_steers,
                )
            case InvalidResponse():
                return _Facts(
                    ObservationStatus.FAILED,
                    accepted=True,
                    resource_id=resource,
                    diagnostic=outcome.detail,
                    reply_errors=_reply_errors(outcome.violations),
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
        # A dispatch is recorded only under an ensured session, so without a binding no
        # dispatch record exists either: the same proof that no provider call began.
        link = (
            None
            if binding is None
            else load_dispatch_record(self._store, bkey, invocation.invocation_id)
        )
        if binding is None or link is None:
            return self._never_dispatched(request, context)
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
            try:
                violation = await self.settlement.settle(
                    AccessKey(binding=bkey, invocation=invocation.invocation_id.root)
                )
            except AccessUnrestorableError as error:
                violation = None
                target = _unrestorable(error, binding.resource_id)
            except AccessSettlementError as error:
                raise _RefusalError(_unknown(str(error), binding.resource_id)) from error
            if violation is not None:
                target = _violated(violation, binding.resource_id)
        else:
            self.settlement.fence(AccessKey(binding=bkey, invocation=invocation.invocation_id.root))
        return self._inspected(request, context, target, link.request_id)

    def _never_dispatched(self, request: InspectTurn, context: ExecutionContext) -> ExecutionResult:
        """Prove the dispatch never began, so core sends it again.

        The dispatch record is written before any provider call, so its absence proves no
        provider call was made. The proof is about the dispatching request, which only core
        can name: without it the turn stays unknown. The owner hears nothing: the turn was
        never interrupted, it is simply sent again.
        """
        if request.dispatch is None:
            raise _RefusalError(_unknown("no dispatch record, and no named dispatching request"))
        own = self._observations.observe(
            ObservationSubject.of(request),
            ObservationFacts(status=ObservationStatus.SUCCEEDED, accepted=True),
            observed_at=context.now_at,
        )
        seen = self._observations.observe(
            ObservationSubject.of(request, request_id=core_named(request.dispatch)),
            # A turn that never reached the provider holds nothing: nothing is left to release.
            ObservationFacts.not_begun("no dispatch record: the turn never reached the provider"),
            observed_at=context.now_at,
        )
        return ExecutionResult(
            observation=RequestObserved(
                observation=own,
                target=TargetObservation(observation=seen, reissue=ReissueProof.NEVER_BEGAN),
            )
        )

    def _inspected(
        self,
        request: InspectTurn,
        context: ExecutionContext,
        target: _Facts,
        target_request: CoreRequestId | None,
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
                released=target.terminal,
                children_complete=target.terminal,
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
                    reply_errors=target.reply_errors,
                ),
            ),
            owner_events=(
                *_refusals(request.invocation, target, context.now_at),
                self._turn_event(request.invocation, seen, target),
            ),
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
            unsettled = access_unproven(
                self._store, AccessKey(binding=bkey, invocation=invocation.invocation_id.root)
            )
            if unsettled is not None:
                return unsettled
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
