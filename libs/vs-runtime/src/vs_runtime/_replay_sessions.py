"""Native replay agent calls through the existing session and access owners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from vs_agent.api import AgentSessionKey, Completed
from vs_core.api import (
    Access,
    CancelTurn,
    CloseSession,
    DecisionId,
    DispatchTurn,
    EnsureSession,
    InputId,
    InputMode,
    InvocationId,
    InvocationRef,
    ObservationStatus,
    RequestId,
    ResourceId,
    RoleId,
    RunId,
    Scope,
    SessionId,
    SessionInput,
    SessionSpec,
    TurnObserved,
    TurnSpec,
)
from vs_core.api.replay import proof_covers
from vs_decode.api import Unreadable, decode_json
from vs_proto.api import TokenUsage, core_vocabulary, replay, validate
from vs_proto.api import replay_agent as pb
from vs_runtime._access_settlement import AccessKey
from vs_runtime._core_requests import ExecutionResult
from vs_runtime._effect_fence import EffectFences
from vs_runtime._replay_operations import ReplayOperationError
from vs_runtime._replay_owner import ReceiptReplayExecutor, ReplayOwnerCodec
from vs_runtime._replay_runtime import ReplayAbsent, ReplayPending, ReplayResult, ReplayUnknown
from vs_runtime._session_requests import invocation_fence, load_session_binding, scope_binding_key
from vs_runtime._session_resolver import session_executors
from vs_runtime.api.core_values import from_proto, to_proto

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from protobuf import Message
    from pydantic import BaseModel

    from vs_agent.api import ClientAgentSessions
    from vs_core.api import ArtifactRef, InputTarget, SchemaRef
    from vs_core.api.replay import ReplayRootReuse
    from vs_project.api import StoreFence
    from vs_proto.api import Span, core_common
    from vs_runtime._core_requests import ExecutionContext, ExecutionOutcome
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._replay_operations import ReplayOperationHost
    from vs_runtime._session_requests import SessionResolver
    from vs_sim.api import BlockingRunner

    type AgentRequest = EnsureSession | DispatchTurn | CancelTurn | CloseSession


@dataclass(frozen=True)
class _AgentDelivery:
    request: AgentRequest
    replacement: bool
    identity: str
    bound: replay.ReplayTurnBound | None


def _retained_yield(
    request: AgentRequest, status: ObservationStatus, value: replay.ReplayTurnWait | None
) -> replay.ReplayTurnWait | None:
    if (
        value is None
        or not isinstance(request, DispatchTurn)
        or status is not ObservationStatus.SUCCEEDED
    ):
        return None
    if value.turn != request.turn.invocation_id.root:
        problem = "retained yield belongs to another physical turn"
        raise ReplayOperationError(problem)
    return value


@dataclass(frozen=True)
class ReplayAgentReply:
    """One exact native output codec for a session resolver's declared reply schema."""

    schema: SchemaRef
    value: type[BaseModel]
    native: type[Message]
    encode: Callable[[pb.ReplayAgentResult, BaseModel | None], Message]


def _session(native: pb.ReplayAgentSession | None, *, fresh_id: str | None = None) -> SessionSpec:
    if native is None:
        problem = "agent replay is missing required message fields"
        raise ReplayOperationError(problem)
    return SessionSpec(
        session_id=SessionId(
            root=fresh_id
            if native.acquisition is pb.ReplaySessionAcquisition.FRESH and fresh_id is not None
            else native.id
        ),
        role_id=RoleId(root=native.role),
        access={
            pb.ReplayAgentAccess.READ_ONLY: Access.READ_ONLY,
            pb.ReplayAgentAccess.WRITE_ARTIFACTS: Access.WRITE_ARTIFACTS,
            pb.ReplayAgentAccess.WRITE_CANDIDATE: Access.WRITE_CANDIDATE,
        }[native.access],
        policy=cast("Literal['fresh', 'reuse']", native.acquisition.name.lower()),
        lifetime=cast("Literal['ephemeral', 'owner']", native.lifetime.name.lower()),
        model=native.model or None,
    )


def _input(native: pb.ReplayAgentInput) -> SessionInput:
    if native.target is None:
        problem = "agent replay is missing required message fields"
        raise ReplayOperationError(problem)
    if native.artifact is None:
        problem = "agent replay is missing required message fields"
        raise ReplayOperationError(problem)
    return SessionInput(
        input_id=InputId(root=native.id),
        target=cast("InputTarget", from_proto(native.target)),
        artifact=cast("ArtifactRef", from_proto(native.artifact)),
        received_at=native.received_at,
        sequence=native.sequence,
        mode=InputMode(native.mode.name.lower().replace("_", "-")),
    )


@dataclass(frozen=True)
class ReplaySessionServices:
    """Host-local agent services, rebound together on Resume."""

    sessions: ClientAgentSessions
    resolver: SessionResolver
    blocking: BlockingRunner
    yielded: Callable[[str], replay.ReplayTurnWait | None] | None = None


class SessionReplayExecutor:
    """Dispatch and recover using one journal and the existing access settlement.

    Both ordinary turns and lifecycle calls come from ``session_executors`` so
    every return, recovery and cancellation judges writes through the same owner.
    A new host supplies newly bound sessions and resolver over the same journal.
    Typed replies are encoded only after that owner validates and releases them.
    """

    def __init__(
        self,
        services: ReplaySessionServices,
        receipts: ReceiptStore,
        host: ReplayOperationHost,
        replies: Sequence[ReplayAgentReply],
    ) -> None:
        sessions, resolver, blocking = services.sessions, services.resolver, services.blocking
        self._sessions, self._resolver, self._receipts, self._blocking = (
            sessions,
            resolver,
            receipts,
            blocking,
        )
        self._host_run_id = host.run_id
        self._replies = {reply.schema: reply for reply in replies}
        if len(self._replies) != len(replies):
            problem = "duplicate replay agent output schema"
            raise ReplayOperationError(problem)
        for reply in replies:
            if resolver.output_schema(reply.schema) is not reply.value:
                problem = "replay agent output differs from the resolver schema"
                raise ReplayOperationError(problem)
        owners = session_executors(sessions, resolver, receipts, None, blocking)
        self._owners = owners

        async def execute(delivery: _AgentDelivery, context: ExecutionContext) -> ExecutionOutcome:
            request = delivery.request
            if request.request_id is None:
                problem = "replay session requests require physical identity"
                raise ReplayOperationError(problem)
            if delivery.replacement and isinstance(request, DispatchTurn):
                ensured = await owners.turns.execute(
                    EnsureSession(
                        request_id=RequestId(root=f"{request.request_id.root}/ensure"),
                        scope=request.scope,
                        admission_id=request.admission_id,
                        spec=request.turn.session,
                    ),
                    context,
                )
                if (
                    not isinstance(ensured, ExecutionResult)
                    or ensured.observation.observation.status is not ObservationStatus.SUCCEEDED
                ):
                    problem = "replacement conversation could not be ensured"
                    raise ReplayOperationError(problem)
            if isinstance(request, (EnsureSession, DispatchTurn)):
                return await owners.turns.execute(request, context)
            return await owners.lifecycle.execute(request, context)

        async def encode(
            delivery: _AgentDelivery, result: ExecutionResult
        ) -> ReplayResult | ReplayUnknown:
            return await self._encode(
                delivery.request,
                result,
                delivery.bound,
                None if services.yielded is None else services.yielded(delivery.identity),
            )

        executor = ReceiptReplayExecutor(
            execute,
            receipts,
            host,
            ReplayOwnerCodec(self._delivery, encode, self._within, self._dispatch_check),
        )
        self._executor = executor

    def _delivery(self, span: Span) -> _AgentDelivery:
        physical = span.replay_physical
        return _AgentDelivery(
            self._request(span),
            physical is not None and physical.ordinal > 0,
            span.id,
            None if span.replay is None else span.replay.turn_bound,
        )

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        return await self._executor.execute(span, fence)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        native = self._native(span)
        if (
            native.action is not None
            and native.action.field == "turn"
            and span.replay_physical is None
        ):
            return ReplayUnknown("agent turn is missing its recorded physical start and deadline")
        return await self._executor.inspect(span, fence)

    def _native(self, span: Span) -> pb.ReplayAgentRequest:
        validate(span)
        operation = span.replay
        if operation is None or operation.policy is None:
            problem = "agent replay requires immutable metadata"
            raise ReplayOperationError(problem)
        policy = operation.policy
        if (
            policy.request is None
            or policy.request.type_url != pb.ReplayAgentRequest.desc().type_name
        ):
            problem = "agent replay request type differs"
            raise ReplayOperationError(problem)
        result = pb.ReplayAgentRequest.from_binary(policy.request.value)
        validate(result)
        if result.to_binary() != result.to_binary(write_unknown_fields=False):
            problem = "agent replay request has unknown fields"
            raise ReplayOperationError(problem)
        expected = pb.ReplayAgentResult
        if result.action is not None and result.action.field == "turn":
            schema = result.action.value.output_schema
            if schema is not None:
                codec = self._replies.get(cast("SchemaRef", from_proto(schema)))
                if codec is None:
                    problem = f"unregistered replay agent reply: {schema.name}"
                    raise ReplayOperationError(problem)
                expected = codec.native
        if policy.reply_type != expected.desc().type_name:
            problem = "agent replay reply type differs"
            raise ReplayOperationError(problem)
        return result

    def _within(self, span: Span) -> float:
        return self._native(span).within_seconds

    def _dispatch_check(self, span: Span) -> None:
        native = self._native(span)
        if native.action is None:
            problem = "agent replay is missing required message fields"
            raise ReplayOperationError(problem)
        if native.action.field != "turn":
            return
        if span.replay_physical is None:
            problem = "agent turn is missing its recorded physical start and deadline"
            raise ReplayOperationError(problem)
        if native.action.value.session is None:
            problem = "agent replay is missing required message fields"
            raise ReplayOperationError(problem)
        role = RoleId(root=native.action.value.session.role)
        timeout = self._resolver.turn_timeout(role)
        if timeout is None or timeout.total_seconds() > native.within_seconds:
            problem = "agent turn timeout exceeds its declared replay bound"
            raise ReplayOperationError(problem)

    def _request(self, span: Span) -> AgentRequest:
        native = self._native(span)
        if native.scope is None:
            problem = "agent replay is missing required message fields"
            raise ReplayOperationError(problem)
        scope = cast("Scope", from_proto(native.scope))
        identity = RequestId(root=span.id)
        admission = None if not native.admission else DecisionId(root=native.admission)
        if native.action is None:
            problem = "agent replay is missing required message fields"
            raise ReplayOperationError(problem)
        match native.action:
            case action if action.field == "ensure":
                return EnsureSession(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    spec=_session(action.value.session, fresh_id=span.id),
                    required_resource=None
                    if not action.value.required_resource
                    else ResourceId(root=action.value.required_resource),
                )
            case action if action.field == "turn":
                turn = action.value
                if turn.workspace is None or turn.output_schema is None:
                    problem = "agent replay is missing required message fields"
                    raise ReplayOperationError(problem)
                schema = cast("SchemaRef", from_proto(turn.output_schema))
                if schema not in self._replies:
                    problem = f"unregistered replay agent reply: {schema.name}"
                    raise ReplayOperationError(problem)
                return DispatchTurn(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    turn=TurnSpec(
                        session=self._turn_session(turn.session, span),
                        invocation_id=InvocationId(root=span.id),
                        workspace=cast("Scope", from_proto(turn.workspace)),
                        prompts=tuple(
                            cast("ArtifactRef", from_proto(item)) for item in turn.prompts
                        ),
                        output_schema=schema,
                        tool_policy=None
                        if turn.tool_policy is None
                        else cast("ArtifactRef", from_proto(turn.tool_policy)),
                        artifact_dependencies=tuple(
                            cast("ArtifactRef", from_proto(item))
                            for item in turn.artifact_dependencies
                        ),
                        charge_class="free" if isinstance(scope.owner, RunId) else "paid",
                        max_turns=turn.max_turns,
                    ),
                    inputs=tuple(map(_input, turn.inputs)),
                    guidance=tuple(map(_input, turn.guidance)),
                )
            case action if action.field == "cancel":
                if action.value.invocation is None:
                    problem = "agent replay is missing required message fields"
                    raise ReplayOperationError(problem)
                return CancelTurn(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    invocation=cast("InvocationRef", from_proto(action.value.invocation)),
                )
            case action if action.field == "close":
                return CloseSession(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    session_id=SessionId(root=action.value.session),
                    resource_id=None
                    if not action.value.resource
                    else ResourceId(root=action.value.resource),
                )
            case _:
                problem = "unsupported replay agent action"
                raise ReplayOperationError(problem)

    @staticmethod
    def _turn_session(native: pb.ReplayAgentSession | None, span: Span) -> SessionSpec:
        spec = _session(native)
        if span.replay_physical is not None and span.replay_physical.ordinal > 0:
            return spec.model_copy(
                update={
                    "session_id": SessionId(root=f"{span.id}/conversation"),
                    "policy": "fresh",
                }
            )
        return spec

    async def retire(self, span: Span, fence: StoreFence, reuse: ReplayRootReuse) -> None:
        """Release and restore an exact withdrawn writer after durable environment proof."""
        proof_covers(span, reuse.proof(), run_id=self._host_run_id)
        request = self._request(span)
        if (
            not isinstance(request, DispatchTurn)
            or span.replay_physical is None
            or not span.replay_physical.has_field("withdrawn_sequence")
        ):
            problem = "agent retirement requires a durably withdrawn physical turn"
            raise ReplayOperationError(problem)
        context = self._executor.context(span, fence)
        if context.lease is None or not context.lease.verify(now_at=context.now_at):
            problem = "agent retirement lost writer exclusion"
            raise ReplayOperationError(problem)
        bkey = scope_binding_key(request.scope, request.turn.session.session_id)
        binding = load_session_binding(self._receipts, bkey)
        if binding is None:
            return
        EffectFences(self._receipts).close(
            invocation_fence(bkey, span.id),
            by=RequestId(root=f"{span.id}/retire"),
            host=context.fence,
        )
        key = AgentSessionKey.parse(binding.session_key)
        await self._blocking.run(self._sessions.cancel, key, span.id)
        await self._blocking.run(self._sessions.release_interrupted, key, span.id)
        await self._blocking.run(self._sessions.release, key)
        await self._owners.turns.settlement.restore_interrupted(
            AccessKey(binding=bkey, invocation=span.id)
        )

    async def _encode(
        self,
        request: AgentRequest,
        result: ExecutionResult,
        bound: replay.ReplayTurnBound | None,
        suspension: replay.ReplayTurnWait | None,
    ) -> ReplayResult | ReplayUnknown:
        seen = result.observation.observation
        if not seen.terminal or seen.status is ObservationStatus.UNKNOWN:
            return ReplayUnknown(seen.diagnostic)
        native = pb.ReplayAgentResult(
            status=core_vocabulary.ObservationStatus[seen.status.name],
            resource=None if seen.resource_id is None else seen.resource_id.root,
            released=seen.released,
            detail=seen.diagnostic,
            turn_bound=bound,
            suspension=_retained_yield(request, seen.status, suspension),
        )
        if isinstance(request, EnsureSession):
            native.session = cast("core_common.SessionId", to_proto(request.spec.session_id))
        if isinstance(request, DispatchTurn):
            native.session = cast(
                "core_common.SessionId", to_proto(request.turn.session.session_id)
            )
            native.invocation = cast(
                "core_common.InvocationRef",
                to_proto(
                    InvocationRef(
                        session_id=request.turn.session.session_id,
                        invocation_id=request.turn.invocation_id,
                        generation=request.scope.generation,
                    )
                ),
            )
        validate(native)
        if not isinstance(request, DispatchTurn):
            return ReplayResult(native)
        codec = self._replies[request.turn.output_schema]
        events = [event for event in result.owner_events if isinstance(event, TurnObserved)]
        if len(events) != 1:
            problem = "agent owner omitted its exact turn observation"
            raise ReplayOperationError(problem)
        value = (
            None
            if events[0].output_json is None
            else decode_json(
                events[0].output_json,
                codec.value,
                source=f"replay agent output {request.request_id}",
                max_bytes=1024 * 1024,
            )
        )
        if isinstance(value, Unreadable):
            return ReplayUnknown(str(value))
        outcome_value = codec.encode(native, value)
        if type(outcome_value) is not codec.native:
            problem = "agent native output type differs"
            raise ReplayOperationError(problem)
        validate(outcome_value)
        binding = load_session_binding(
            self._receipts, scope_binding_key(request.scope, request.turn.session.session_id)
        )
        if binding is None:
            return ReplayResult(outcome_value, paid=seen.accepted)
        outcome = await self._blocking.run(
            self._sessions.inspect,
            AgentSessionKey.parse(binding.session_key),
            request.turn.invocation_id.root,
        )
        if not isinstance(outcome, Completed):
            return ReplayResult(outcome_value, paid=seen.accepted)
        usage = outcome.result.usage
        tokens = TokenUsage(
            input=usage.input_tokens,
            cache_creation=usage.cache_creation_input_tokens,
            cache_read=usage.cache_read_input_tokens,
            output=usage.output_tokens,
            reasoning=usage.reasoning_output_tokens,
            cost_usd=usage.total_cost_usd,
            duration_ms=usage.duration_ms,
        )
        validate(tokens)
        return ReplayResult(outcome_value, paid=True, cost_usd=usage.total_cost_usd, usage=tokens)
