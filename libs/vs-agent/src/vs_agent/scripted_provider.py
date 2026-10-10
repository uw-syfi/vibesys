"""A scripted provider: run a whole agent stack without a CLI, a model, or a network.

:class:`FakeProvider` is a :class:`~vs_agent.session_launch.SessionLauncher`
whose sessions are real ``agentshim.Session`` objects over a scripted
``agentshim`` transport. Every event a scripted turn produces therefore takes
the production route: library event, :mod:`vs_agent.shim_translation`, the
``AgentObserver`` its caller passed, the caller-owned agent event sink and any
composed adapters. The fake never writes an event, a state file, or a log
itself; anything it wrote directly would be a path integration tests then stop
covering.

A caller scripts a turn explicitly, as a list of library events built with the
module-level helpers below (:func:`assistant_text`, :func:`thinking`,
:func:`tool_call`, :func:`tool_result`, :func:`todo_write`, :func:`usage`),
rather than through count knobs: what a test asserts on is what it wrote.

Structured turns require the caller to supply ``answer``. The fake has no
knowledge of application response schemas or policy defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import agentshim
from pydantic import BaseModel, TypeAdapter

from vs_agent.contracts import (
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentQuotaError,
    AgentSessionSpec,
    QuotaCondition,
)
from vs_agent.shim_translation import default_agent_path
from vs_agent.shim_turns import LaunchedSession, SteerLedger, TurnEvents
from vs_sim.api import OsThreads, Threads

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vs_agent.contracts import (
        AgentObserver,
        AgentTurnRequest,
        AgentTurnResult,
    )

FAKE_CAPABILITIES = AgentCapabilities(
    tool_servers=True,
    nested_read_only_paths=True,
    hidden_paths=True,
    host_path_grants=True,
    container_execution=True,
    timeouts=True,
    session_reuse=True,
    provider_session_resume=True,
)
"""What the fake can honor without weakening the semantics it was handed.

The fake starts no process and touches no path outside its own scripted
events, so every sandbox restriction in an ``AgentExecutionPolicy`` is
satisfied vacuously: there is nothing running that could reach a restricted
path. The fake therefore accepts any policy rather than rejecting
configurations a real launcher would have run.
"""

# The todo tool whose call the callback layer turns into a todo snapshot.
# Emitting the tool call (rather than a todo event) is the point: the fake
# exercises the same provider-vocabulary translation a real provider does.
_TODO_TOOL = "TodoWrite"

# The tool name every scripted tool call/result pair uses. Correlation between
# a call and its result happens by tool name (see ``AgentLogger``'s pending
# call-id queue), so the builders below share one fixed name rather than
# asking every caller to keep a call and its result in sync themselves.
_TOOL_NAME = "Bash"

_USAGE_DURATION_MS = 10

type Answer = (
    BaseModel | Mapping[str, object] | str | AgentOutputSchemaError | AgentQuotaError | None
)


class FakeProviderError(RuntimeError):
    """A fake provider turn was configured with something it cannot run."""

    @classmethod
    def conflicting_turn_inputs(cls) -> FakeProviderError:
        """Describe both mutually exclusive turn inputs being supplied."""
        return cls("FakeProvider accepts `turn` or `turns`, not both")

    @classmethod
    def no_turns(cls) -> FakeProviderError:
        """Describe an empty per-turn script."""
        return cls("`turns` must contain at least one turn")

    @classmethod
    def cancelled(cls) -> FakeProviderError:
        """Describe a turn the caller cancelled while it ran."""
        return cls("fake agent turn was cancelled")

    @classmethod
    def provider_closed(cls) -> FakeProviderError:
        """Describe session creation after the fake provider was closed."""
        return cls("fake agent provider is closed")

    @classmethod
    def missing_structured_answer(cls, schema_name: str) -> FakeProviderError:
        """Describe a structured turn without an explicit fake answer."""
        return cls(
            f"no fake answer was supplied for response schema {schema_name!r}; "
            "pass answer= when constructing FakeProvider"
        )


class FakeCancels:
    """Cancellation requests every session of one fake provider received."""

    def __init__(self, threads: Threads) -> None:
        """Start with no request."""
        self._lock = threads.lock()
        self._count = 0
        self._event = threads.event()

    def record(self) -> None:
        """Count one request and release anything waiting for it."""
        with self._lock:
            self._count += 1
        self._event.set()

    @property
    def count(self) -> int:
        """How many requests arrived."""
        with self._lock:
            return self._count

    def wait(self, timeout: float) -> bool:
        """Block a turn until a request arrives; ``timeout`` only guards a lost cancel."""
        return self._event.wait(timeout)


@dataclass(frozen=True, slots=True)
class FakeTurnScript:
    """Per-turn replies and an optional successful-turn renewal threshold."""

    answers: tuple[Answer, ...]
    reset_after_turn: int | None = None

    def __post_init__(self) -> None:
        """Reject empty scripts and invalid renewal boundaries."""
        if not self.answers:
            raise FakeProviderError.no_turns()
        if self.reset_after_turn is not None and (
            isinstance(self.reset_after_turn, bool) or self.reset_after_turn < 1
        ):
            message = "reset_after_turn must be a positive integer"
            raise ValueError(message)


@dataclass(slots=True)
class _InFlight:
    """The VibeSys request of the turn running now, which the library never sees.

    It also carries the :class:`Threads` its session's locks come from.
    """

    threads: Threads
    request: AgentTurnRequest | None = None
    on_turn: Callable[[AgentTurnRequest], None] | None = None
    barrier_error: BaseException | None = None


class _ScriptedConversation:
    """One scripted provider conversation: events, then the next answer."""

    def __init__(
        self,
        transport: _ScriptedTransport,
        spec: agentshim.ConversationSpec,
    ) -> None:
        self._transport = transport
        self._id = spec.resume_id
        self._closed = False

    @property
    def conversation_id(self) -> str | None:
        return self._id

    def turn(
        self,
        request: agentshim.TurnRequest,
        emit: Callable[[agentshim.AgentEvent], None],
    ) -> agentshim.TurnResult:
        del request
        if self._closed:
            message = "conversation is closed"
            raise agentshim.SessionStateError(message)
        transport = self._transport
        flight = transport.in_flight
        if flight.on_turn is not None and flight.request is not None:
            # The library has already checked strict-turn identity and counted
            # this turn as in flight, so the barrier sees what production would.
            try:
                flight.on_turn(flight.request)
            except Exception as error:
                # The library would re-wrap it as a spawn failure; the test
                # raised it to be seen as is, so ScriptedSession.run_turn
                # re-raises this original.
                flight.barrier_error = error
                raise
        resumed = self._id is not None
        invocation = transport.next_invocation()
        events = transport.turns[min(invocation, len(transport.turns)) - 1]
        for event in events:
            emit(event)
        if self._id is None:
            self._id = transport.new_conversation_id()
        answer = transport.answers[min(invocation, len(transport.answers)) - 1]
        if isinstance(answer, AgentOutputSchemaError):
            raise agentshim.TurnFailedError(
                str(answer), kind=agentshim.FailureKind.SCHEMA, detail=answer.detail
            )
        if isinstance(answer, AgentQuotaError):
            raise _failure_for(answer, emit)
        usage, cost = _last_usage(events)
        return agentshim.TurnResult(
            text=_turn_text(answer, transport.in_flight.request),
            structured_output=None,
            session_id=self._id,
            resumed=resumed,
            usage=usage,
            cost_usd=cost,
            duration_ms=_USAGE_DURATION_MS if cost is not None else 0,
            exit_code=0,
        )

    def interrupt(self) -> None:
        """Scripted turns finish on their own; there is nothing to stop."""

    def close(self) -> None:
        self._closed = True


class _ScriptedTransport:
    """An ``agentshim.Transport`` that plays one fake session's scripted turns."""

    def __init__(
        self,
        *,
        turns: tuple[tuple[agentshim.AgentEvent, ...], ...],
        script: FakeTurnScript,
        profile: agentshim.ProviderProfile,
        in_flight: _InFlight,
        ids: Callable[[], str],
    ) -> None:
        self.turns = turns
        self.answers = script.answers
        self.in_flight = in_flight
        self._profile = profile
        self._ids = ids
        self._invocations = 0
        self._lock = in_flight.threads.lock()

    @property
    def profile(self) -> agentshim.ProviderProfile:
        return self._profile

    def open(self, spec: agentshim.ConversationSpec) -> agentshim.Conversation:
        return _ScriptedConversation(self, spec)

    def next_invocation(self) -> int:
        with self._lock:
            self._invocations += 1
            return self._invocations

    def new_conversation_id(self) -> str:
        return self._ids()


@dataclass(slots=True)
class _Hooks:
    """What a session reports to and takes from its test."""

    on_turn: Callable[[AgentTurnRequest], None] | None
    cancels: FakeCancels
    resumed: list[str]


class ScriptedSession(LaunchedSession):
    """A launched session that reports its turns and cancels to its fake provider."""

    def __init__(
        self,
        *,
        launched: LaunchedSession,
        hooks: _Hooks,
        in_flight: _InFlight,
    ) -> None:
        """Wrap a freshly launched fake session with its provider's hooks."""
        super().__init__(
            session=launched.session,
            profile=launched.profile,
            spec=launched.spec,
            events=launched.events,
            steers=launched.steers,
            agent_path=launched.agent_path,
            timeout=launched.timeout,
            log=launched.log,
        )
        self.hooks = hooks
        self.in_flight = in_flight
        self.turns_in_progress = 0
        self.state_lock = in_flight.threads.lock()

    @override
    def run_turn(
        self, request: AgentTurnRequest, observer: AgentObserver | None = None
    ) -> AgentTurnResult:
        """Run the turn for real; the ``on_turn`` barrier fires inside the conversation."""
        with self.state_lock:
            self.turns_in_progress += 1
        try:
            self.in_flight.request = request
            self.in_flight.barrier_error = None
            try:
                return super().run_turn(request, observer)
            except Exception:
                if self.in_flight.barrier_error is not None:
                    raise self.in_flight.barrier_error from None
                raise
        finally:
            self.in_flight.request = None
            with self.state_lock:
                self.turns_in_progress -= 1

    @override
    def cancel(self) -> None:
        """Record a cancel that arrives while a turn is held open.

        Cancelling an idle session is a no-op, as in production. Scripted turns
        finish on their own; only a turn an ``on_turn`` barrier holds open can
        be in flight, and the barrier learns of the cancel through
        :meth:`FakeProvider.hold_until_cancelled`.
        """
        with self.state_lock:
            running = self.turns_in_progress > 0
        if running:
            self.hooks.cancels.record()
        super().cancel()

    @override
    def adopt(self, session_id: str) -> bool:
        """Adopt ``session_id`` so a resumed run's continuity is observable."""
        adopted = super().adopt(session_id)
        if adopted:
            self.hooks.resumed.append(session_id)
        return adopted


class FakeProvider:
    """Open scripted sessions that stream fixed turns instead of running an agent."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-168417 [PLR0913]; Preserve FakeProvider.__init__'s named-argument contract because tests pass these independent settings directly.
        self,
        *,
        turn: Sequence[agentshim.AgentEvent] | None = None,
        turns: Sequence[Sequence[agentshim.AgentEvent]] | None = None,
        answer: BaseModel | Mapping[str, object] | str | None = None,
        script: FakeTurnScript | None = None,
        on_turn: Callable[[AgentTurnRequest], None] | None = None,
        threads: Threads | None = None,
    ) -> None:
        """Create a provider whose sessions emit ``turn``/``turns`` and answer with ``answer``.

        ``turn`` is the event sequence emitted for every turn a session runs.
        ``turns`` is a sequence of distinct per-turn event sequences: a
        session's Nth turn emits ``turns[N - 1]``, and once a session has run
        more turns than ``turns`` has entries, its last entry keeps repeating.
        ``turn=[...]`` is sugar for ``turns=[[...]]``. ``on_turn`` observes
        each accepted request and may block at a deterministic barrier or
        raise a scheduled boundary failure. Passing neither runs a turn that
        emits no events. ``answer`` sets the text or structured payload every
        turn returns. It is required for a structured turn, keeping
        application response policy out of this fake. ``script.answers``
        supplies per-turn payloads or provider schema rejections; the last
        entry repeats. A schema rejection keeps the provider conversation,
        just like production, and can be followed by a correction turn.
        ``script.reset_after_turn`` retires a completed conversation after
        that turn, as production budget renewal does. Strict continuations
        suppress renewal.
        """
        if turn is not None and turns is not None:
            raise FakeProviderError.conflicting_turn_inputs()
        if turn is not None:
            resolved_turns: tuple[tuple[agentshim.AgentEvent, ...], ...] = (tuple(turn),)
        elif turns is not None:
            resolved_turns = tuple(tuple(one_turn) for one_turn in turns)
            if not resolved_turns:
                raise FakeProviderError.no_turns()
        else:
            resolved_turns = ((),)
        if script is not None and answer is not None:
            raise FakeProviderError.conflicting_turn_inputs()
        self._threads = threads or OsThreads()
        self._cancels = FakeCancels(self._threads)
        self._resumed: list[str] = []
        self._on_turn = on_turn
        self._turns = resolved_turns
        self._script = script if script is not None else FakeTurnScript((answer,))
        self._sessions: list[ScriptedSession] = []
        self._session_count = 0
        self._lock = self._threads.lock()
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe what the fake honors; see :data:`FAKE_CAPABILITIES`."""
        return FAKE_CAPABILITIES

    @property
    def cancel_count(self) -> int:
        """Cancellation requests received by every session of this provider."""
        return self._cancels.count

    def hold_until_cancelled(self, timeout: float) -> None:
        """Hold an ``on_turn`` barrier open until a cancel arrives, then end the turn with an error.

        A production launcher ends a cancelled in-flight turn with an error.
        ``timeout`` only guards a lost cancel, which returns without raising.
        """
        if self._cancels.wait(timeout):
            raise FakeProviderError.cancelled()

    @property
    def resumed_session_ids(self) -> tuple[str, ...]:
        """Return provider session IDs adopted by opened sessions, in order."""
        return tuple(self._resumed)

    def launch(self, spec: AgentSessionSpec) -> LaunchedSession:
        """Open one scripted conversation for ``spec``."""
        with self._lock:
            if self._closed:
                raise FakeProviderError.provider_closed()
            self._session_count += 1
            ordinal = self._session_count
        profile = _profile(spec.provider, self._script.reset_after_turn)
        in_flight = _InFlight(self._threads, on_turn=self._on_turn)
        transport = _ScriptedTransport(
            turns=self._turns,
            script=self._script,
            profile=profile,
            in_flight=in_flight,
            ids=lambda: f"fake-{spec.role}-{ordinal}",
        )
        steers = SteerLedger(self._threads)
        events = TurnEvents(steers)
        agent = agentshim.Agent(
            transport,
            permissions=agentshim.NativePermissions.bypass(),
            approvals=agentshim.ApprovalPolicy.DENY,
            retry=agentshim.RetryPolicy(delays=()),
            event_handlers=[events],
        )
        session = ScriptedSession(
            launched=LaunchedSession(
                session=agent.session(str(spec.workspace)),
                profile=profile,
                spec=spec,
                events=events,
                steers=steers,
                agent_path=default_agent_path,
                timeout=None,
                log=_ignore,
            ),
            hooks=_Hooks(self._on_turn, self._cancels, self._resumed),
            in_flight=in_flight,
        )
        with self._lock:
            self._sessions.append(session)
        return session

    def close(self) -> None:
        """Close every session this provider opened, idempotently."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()


def _ignore(_message: str) -> None:
    """Discard diagnostics: a fake has no run log."""


def _profile(provider: str, reset_after_turn: int | None) -> agentshim.ProviderProfile:
    """A resumable, non-steerable profile named for the provider a spec asks for."""
    base = agentshim.get_provider("claude").profile
    renewal = (
        base.renewal
        if reset_after_turn is None
        else agentshim.RenewalBudget(max_turns=reset_after_turn)
    )
    return replace(
        base,
        name=provider,
        display_name=provider.title(),
        binary=provider,
        supports_steer=False,
        renewal=renewal,
    )


def _failure_for(
    error: AgentQuotaError, emit: Callable[[agentshim.AgentEvent], None]
) -> agentshim.TurnFailedError:
    """Express a scripted capacity limit the way a provider reports one."""
    if error.condition is QuotaCondition.RATE_LIMITED:
        emit(
            agentshim.RateLimitStatus(
                window="scripted",
                used_fraction=1.0,
                resets_at=error.resets_at,
                exhausted=True,
            )
        )
        kind = agentshim.FailureKind.TRANSIENT
    else:
        if error.resets_at is not None:
            emit(
                agentshim.RateLimitStatus(
                    window="scripted",
                    used_fraction=1.0,
                    resets_at=error.resets_at,
                    exhausted=True,
                )
            )
        kind = agentshim.FailureKind.USAGE_LIMIT
    return agentshim.TurnFailedError(str(error), kind=kind, detail=error.detail)


def _last_usage(
    events: tuple[agentshim.AgentEvent, ...],
) -> tuple[agentshim.ProviderUsage, float | None]:
    """Return the last scripted usage report, or an unmeasured one."""
    for event in reversed(events):
        if isinstance(event, agentshim.UsageReport):
            return event.usage, event.cost_usd
    return agentshim.ProviderUsage(increment_known=False), None


def _turn_text(
    answer: BaseModel | Mapping[str, object] | str | None, request: AgentTurnRequest | None
) -> str:
    if answer is not None:
        return _serialize_answer(answer)
    schema = None if request is None else request.output_schema
    if schema is None:
        label = None if request is None else request.label
        return f"Fake agent completed {label or 'a turn'} with no workspace changes."
    raise FakeProviderError.missing_structured_answer(schema.__name__)


def _serialize_answer(answer: BaseModel | Mapping[str, object] | str) -> str:
    if isinstance(answer, BaseModel):
        return answer.model_dump_json()
    if isinstance(answer, str):
        return answer
    return TypeAdapter(Any).dump_json(answer).decode()


# --- event builders ---------------------------------------------------------


def assistant_text(text: str) -> agentshim.AgentEvent:
    """Return one assistant-text chunk event."""
    return agentshim.AssistantText(text=text)


def thinking(text: str) -> agentshim.AgentEvent:
    """Return one reasoning-chunk event, published on the analysis channel."""
    return agentshim.Reasoning(text=text)


def tool_call(
    name: str, args: Mapping[str, object], *, call_id: str | None = None
) -> agentshim.AgentEvent:
    """Return one tool-invocation event for tool ``name`` with ``args``.

    The sink correlates a call with its result by tool name and emission
    order (see ``AgentLogger``), not by ``call_id``.
    """
    return agentshim.ToolCall(tool_id=call_id, tool=name, args=dict(args))


def tool_result(
    content: str, *, is_error: bool = False, call_id: str | None = None
) -> agentshim.AgentEvent:
    """Return one tool-result event carrying ``content``.

    Pairs with :func:`tool_call` under the shared fixed tool name so the two
    correlate through the sink the same way a real provider's matched call and
    result do.
    """
    return agentshim.ToolResult(
        tool_id=call_id,
        tool=_TOOL_NAME,
        stdout="" if is_error else content,
        stderr=content if is_error else "",
        exit_code=1 if is_error else 0,
        duration_s=0.0,
    )


def todo_write(items: Sequence[tuple[str, str]]) -> agentshim.AgentEvent:
    """Return a todo snapshot delivered the way a provider delivers one: a tool call.

    ``items`` is a sequence of ``(content, status)`` pairs. Todos reach the
    sink through ``todos_from_tool_call``, so emitting the tool call keeps the
    fake on the same translation path as a real provider.
    """
    todos = [{"content": content, "status": status} for content, status in items]
    return agentshim.ToolCall(tool_id=None, tool=_TODO_TOOL, args={"todos": todos})


def usage(*, input_tokens: int, output_tokens: int) -> agentshim.AgentEvent:
    """Return one usage-update event."""
    return agentshim.UsageReport(
        usage=agentshim.ProviderUsage(
            tokens=agentshim.TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
        cost_usd=0.0,
    )


class HandSession(LaunchedSession):
    """A launched session whose behavior a test programs by overriding its methods.

    For tests of :class:`~vs_agent.client.AgentClient`'s own session policy
    (caching, checkpoints, eviction, cancellation order), where the turn
    outcome, not the provider exchange behind it, is the input. It wraps an
    inert library session, so nothing runs unless a subclass says so.
    """

    def __init__(
        self, spec: AgentSessionSpec | None = None, *, threads: Threads | None = None
    ) -> None:
        """Wrap an inert library session for ``spec`` (a placeholder when omitted)."""
        threads = threads or OsThreads()
        resolved = spec if spec is not None else _placeholder_spec()
        profile = _profile(resolved.provider, None)
        steers = SteerLedger(threads)
        events = TurnEvents(steers)
        agent = agentshim.Agent(
            _ScriptedTransport(
                turns=((),),
                script=FakeTurnScript((None,)),
                profile=profile,
                in_flight=_InFlight(threads),
                ids=lambda: "hand-session",
            ),
            permissions=agentshim.NativePermissions.bypass(),
            approvals=agentshim.ApprovalPolicy.DENY,
            retry=agentshim.RetryPolicy(delays=()),
            event_handlers=[events],
        )
        super().__init__(
            session=agent.session(str(resolved.workspace)),
            profile=profile,
            spec=resolved,
            events=events,
            steers=steers,
            agent_path=default_agent_path,
            timeout=None,
            log=_ignore,
        )


def _placeholder_spec() -> AgentSessionSpec:
    return AgentSessionSpec(
        role="role",
        provider="codex",
        workspace=Path("/workspace"),
        policy=AgentExecutionPolicy(),
    )
