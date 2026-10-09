"""Fake implementation of the stateful agent-driver contract.

The fake exists so a run can be exercised end to end without an agent CLI,
a model, or a network. It is a driver, not a shortcut around one: every event
it produces leaves through the ``AgentObserver`` its caller passed to
:meth:`FakeSession.run_turn`, which means it reaches the caller-owned agent
event sink and any composed adapters by exactly the same route a real driver's
events take. The fake never writes an event, a state file, or a log itself.
Anything it wrote directly would be a path integration tests then stop
covering.

A caller scripts a turn explicitly, as a list of :class:`~vs_agent.contracts.
AgentEvent` values built with the module-level helpers below
(:func:`assistant_text`, :func:`thinking`, :func:`tool_call`,
:func:`tool_result`, :func:`todo_write`, :func:`usage`), rather than through
count knobs: what a test asserts on is what it wrote.

Structured turns require the caller to supply ``answer``. The fake has no
knowledge of application response schemas or policy defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter

from vs_agent.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentOutputSchemaError,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
    SessionDisposition,
)
from vs_agent.events import CommandResultPayload
from vs_agent.session_errors import SessionResumeError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vs_agent.contracts import AgentObserver

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
configurations a real driver would have run.
"""

# The todo tool whose call the callback layer turns into a todo snapshot.
# Emitting the tool call (rather than a todo event) is the point: the fake
# exercises the same provider-vocabulary translation a real driver does.
_TODO_TOOL = "TodoWrite"

# The tool name every scripted tool call/result pair uses. Correlation between
# a call and its result happens by tool name (see ``AgentLogger``'s pending
# call-id queue), so the builders below share one fixed name rather than
# asking every caller to keep a call and its result in sync themselves.
_TOOL_NAME = "Bash"


class FakeDriverError(RuntimeError):
    """A fake driver turn was configured with something it cannot run."""

    @classmethod
    def session_closed(cls) -> FakeDriverError:
        """Describe a turn requested after its fake session was closed."""
        return cls("fake agent session is closed")

    @classmethod
    def conflicting_turn_inputs(cls) -> FakeDriverError:
        """Describe both mutually exclusive turn inputs being supplied."""
        return cls("FakeDriver accepts `turn` or `turns`, not both")

    @classmethod
    def no_turns(cls) -> FakeDriverError:
        """Describe an empty per-turn script."""
        return cls("`turns` must contain at least one turn")

    @classmethod
    def cancelled(cls) -> FakeDriverError:
        """Describe a turn the caller cancelled while it ran."""
        return cls("fake agent turn was cancelled")

    @classmethod
    def driver_closed(cls) -> FakeDriverError:
        """Describe session creation after the fake driver was closed."""
        return cls("fake agent driver is closed")

    @classmethod
    def missing_structured_answer(cls, schema_name: str) -> FakeDriverError:
        """Describe a structured turn without an explicit fake answer."""
        return cls(
            f"no fake answer was supplied for response schema {schema_name!r}; "
            "pass answer= when constructing FakeDriver"
        )


class FakeCancels:
    """Cancellation requests every session of one fake driver received."""

    def __init__(self) -> None:
        """Start with no request."""
        self._lock = Lock()
        self._count = 0
        self._event = Event()

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

    answers: tuple[BaseModel | Mapping[str, object] | str | AgentOutputSchemaError | None, ...]
    reset_after_turn: int | None = None

    def __post_init__(self) -> None:
        """Reject empty scripts and invalid renewal boundaries."""
        if not self.answers:
            raise FakeDriverError.no_turns()
        if self.reset_after_turn is not None and (
            isinstance(self.reset_after_turn, bool) or self.reset_after_turn < 1
        ):
            message = "reset_after_turn must be a positive integer"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class _TurnHooks:
    """What a session reports to and takes from its test: a turn barrier and cancel log."""

    on_turn: Callable[[AgentTurnRequest], None] | None
    cancels: FakeCancels


class FakeSession:
    """One fake conversation. Its state is which scripted turn runs next."""

    def __init__(
        self,
        *,
        spec: AgentSessionSpec,
        turns: tuple[tuple[AgentEvent, ...], ...],
        script: FakeTurnScript,
        resumed_session_ids: list[str],
        hooks: _TurnHooks | None = None,
    ) -> None:
        """Create a session bound to ``turns``/``answer`` for ``spec``'s role."""
        hooks = hooks if hooks is not None else _TurnHooks(None, FakeCancels())
        self._cancels = hooks.cancels
        self._spec = spec
        self._on_turn = hooks.on_turn
        self._turns = turns
        self._answers = script.answers
        self._resumed_session_ids = resumed_session_ids
        self._reset_after_turn = script.reset_after_turn
        self._invocations = 0
        self._successful_turns = 0
        self._closed = False
        self._provider_session_id: str | None = None
        self._turns_in_progress = 0
        self._state_lock = Lock()

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Emit the next scripted turn's events, then answer it."""
        with self._state_lock:
            if self._closed:
                raise FakeDriverError.session_closed()
            expected = request.expected_provider_session_id
            if expected is not None and self._provider_session_id != expected:
                raise SessionResumeError(
                    expected, "session has not adopted the expected conversation"
                )
            self._turns_in_progress += 1
        try:
            if self._on_turn is not None:
                self._on_turn(request)
            self._invocations += 1
            events = self._turns[min(self._invocations, len(self._turns)) - 1]
            for event in events:
                if observer is not None:
                    observer.on_event(event)
            provider_session_id = (
                self._provider_session_id or f"fake-{self._spec.role}-{id(self):x}"
            )
            with self._state_lock:
                self._provider_session_id = provider_session_id
            answer = self._answers[min(self._invocations, len(self._answers)) - 1]
            if isinstance(answer, AgentOutputSchemaError):
                raise answer
            self._successful_turns += 1
            resets = (
                self._reset_after_turn is not None
                and self._successful_turns >= self._reset_after_turn
                and expected is None
                and not request.require_provider_checkpoint
            )
            if resets:
                with self._state_lock:
                    self._provider_session_id = None
            return AgentTurnResult(
                text=_turn_text(answer, request),
                disposition=(
                    SessionDisposition.RESET_REQUIRED if resets else SessionDisposition.REUSABLE
                ),
                usage=_turn_usage(events),
                provider_session_id=provider_session_id,
            )
        finally:
            with self._state_lock:
                self._turns_in_progress -= 1

    def cancel(self) -> None:
        """Record the request and end a turn blocked in ``on_turn`` with an error.

        Scripted turns finish on their own; only a turn an ``on_turn`` barrier holds
        open can be in flight, and the barrier learns of the cancel through
        :meth:`FakeDriver.hold_until_cancelled`.
        """
        with self._state_lock:
            running = self._turns_in_progress > 0
        if running:  # cancelling an idle session is a no-op, as in production
            self._cancels.record()

    def close(self) -> None:
        """Release this session. Idempotent; the fake owns no resources."""
        self._closed = True

    def resume_provider_session(self, session_id: str) -> bool:
        """Adopt ``session_id`` so a resumed run's continuity is observable."""
        with self._state_lock:
            if self._provider_session_id is not None or self._turns_in_progress:
                return False
            self._provider_session_id = session_id
            self._resumed_session_ids.append(session_id)
            return True


class FakeDriver:
    """Create fake sessions that stream scripted turns instead of running an agent."""

    def __init__(
        self,
        *,
        turn: Sequence[AgentEvent] | None = None,
        turns: Sequence[Sequence[AgentEvent]] | None = None,
        answer: BaseModel | Mapping[str, object] | str | None = None,
        script: FakeTurnScript | None = None,
        on_turn: Callable[[AgentTurnRequest], None] | None = None,
    ) -> None:
        """Create a driver whose sessions emit ``turn``/``turns`` and answer with ``answer``.

        ``turn`` is the event sequence emitted for every turn a session runs.
        ``turns`` is a sequence of distinct per-turn event sequences: a
        session's Nth ``run_turn`` call emits ``turns[N - 1]``, and once a
        session has run more turns than ``turns`` has entries, its last entry
        keeps repeating. ``turn=[...]`` is sugar for ``turns=[[...]]``.
        ``on_turn`` observes each accepted request and may block at a deterministic
        barrier or raise a scheduled boundary failure.
        Passing neither runs a turn that emits no events. ``answer`` sets the
        text or structured payload every turn returns. It is required for a
        structured turn, keeping application response policy out of this fake.
        ``script.answers`` supplies per-turn payloads or provider schema rejections;
        the last entry repeats. A schema rejection keeps the provider conversation,
        just like production, and can be followed by a correction turn.
        ``script.reset_after_turn`` retires a completed conversation after that turn,
        as production budget renewal does. Strict continuations suppress renewal.
        """
        if turn is not None and turns is not None:
            raise FakeDriverError.conflicting_turn_inputs()
        if turn is not None:
            resolved_turns: tuple[tuple[AgentEvent, ...], ...] = (tuple(turn),)
        elif turns is not None:
            resolved_turns = tuple(tuple(one_turn) for one_turn in turns)
            if not resolved_turns:
                raise FakeDriverError.no_turns()
        else:
            resolved_turns = ((),)
        if script is not None and answer is not None:
            raise FakeDriverError.conflicting_turn_inputs()
        self._on_turn = on_turn
        self._cancels = FakeCancels()
        self._turns = resolved_turns
        self._script = script if script is not None else FakeTurnScript((answer,))
        self._sessions: list[FakeSession] = []
        self._resumed_session_ids: list[str] = []
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe what the fake honors; see :data:`FAKE_CAPABILITIES`."""
        return FAKE_CAPABILITIES

    @property
    def cancel_count(self) -> int:
        """Cancellation requests received by every session of this driver."""
        return self._cancels.count

    def hold_until_cancelled(self, timeout: float) -> None:
        """Hold an ``on_turn`` barrier open until a cancel arrives, then end the turn with an error.

        A production driver ends a cancelled in-flight turn with an error. ``timeout``
        only guards a lost cancel, which returns without raising.
        """
        if self._cancels.wait(timeout):
            raise FakeDriverError.cancelled()

    @property
    def resumed_session_ids(self) -> tuple[str, ...]:
        """Return provider session IDs adopted by created sessions, in order."""
        return tuple(self._resumed_session_ids)

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create one fake conversation for ``spec``."""
        if self._closed:
            raise FakeDriverError.driver_closed()
        session = FakeSession(
            spec=spec,
            turns=self._turns,
            script=self._script,
            resumed_session_ids=self._resumed_session_ids,
            hooks=_TurnHooks(self._on_turn, self._cancels),
        )
        self._sessions.append(session)
        return session

    def close(self) -> None:
        """Close every session this driver created, idempotently."""
        if self._closed:
            return
        self._closed = True
        for session in self._sessions:
            session.close()
        self._sessions.clear()


# --- event builders ---------------------------------------------------------


def assistant_text(text: str) -> AgentEvent:
    """Return one assistant-text chunk event."""
    return AgentEvent(kind=AgentEventKind.TEXT, text=text)


def thinking(text: str) -> AgentEvent:
    """Return one reasoning-chunk event, published on the analysis channel."""
    return AgentEvent(kind=AgentEventKind.THINKING, text=text)


def tool_call(name: str, args: Mapping[str, object], *, call_id: str | None = None) -> AgentEvent:
    """Return one tool-invocation event for tool ``name`` with ``args``.

    ``call_id`` is carried in the event payload for a caller that wants to
    inspect it directly; the sink correlates a call with its result by tool
    name and emission order (see ``AgentLogger``), not by this field.
    """
    payload: dict[str, object] = {"tool": name, "args": dict(args)}
    if call_id is not None:
        payload["call_id"] = call_id
    return AgentEvent(kind=AgentEventKind.TOOL_CALL, payload=payload)


def tool_result(content: str, *, is_error: bool = False, call_id: str | None = None) -> AgentEvent:
    """Return one tool-result event carrying ``content``.

    Pairs with :func:`tool_call` under the shared fixed tool name so the two
    correlate through the sink the same way a real driver's matched call and
    result do.
    """
    stdout = "" if is_error else content
    stderr = content if is_error else ""
    exit_code = 1 if is_error else 0
    payload: dict[str, object] = {
        "tool": _TOOL_NAME,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration": 0.0,
        "result_payload": CommandResultPayload(
            stdout=stdout, stderr=stderr, exit_code=exit_code, duration=0.0
        ),
    }
    if call_id is not None:
        payload["call_id"] = call_id
    return AgentEvent(kind=AgentEventKind.TOOL_RESULT, text=content, payload=payload)


def todo_write(items: Sequence[tuple[str, str]]) -> AgentEvent:
    """Return a todo snapshot delivered the way a provider delivers one: a tool call.

    ``items`` is a sequence of ``(content, status)`` pairs. Todos reach the
    sink through ``todos_from_tool_call``, so emitting the tool call keeps the
    fake on the same translation path as a real driver.
    """
    todos = [{"content": content, "status": status} for content, status in items]
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        payload={"tool": _TODO_TOOL, "args": {"todos": todos}},
    )


def usage(*, input_tokens: int, output_tokens: int, model: str = "fake-model") -> AgentEvent:
    """Return one usage-update event.

    ``model`` is attributed for a caller that wants to inspect the event
    directly; the sink's reported model comes from the ``AgentClient`` the
    fake is driving, not from this field.
    """
    return AgentEvent(
        kind=AgentEventKind.USAGE,
        payload={"model": model},
        usage=AgentUsage(
            input_tokens=input_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=output_tokens,
            total_cost_usd=0.0,
            duration_ms=10,
        ),
    )


# --- turn resolution ---------------------------------------------------------


def _turn_usage(events: tuple[AgentEvent, ...]) -> AgentUsage:
    """Return the last scripted usage update, or an empty usage record."""
    for event in reversed(events):
        if event.kind is AgentEventKind.USAGE and event.usage is not None:
            return event.usage
    return AgentUsage()


def _turn_text(
    answer: BaseModel | Mapping[str, object] | str | None,
    request: AgentTurnRequest,
) -> str:
    if answer is not None:
        return _serialize_answer(answer)
    schema = request.output_schema
    if schema is None:
        return f"Fake agent completed {request.label or 'a turn'} with no workspace changes."
    raise FakeDriverError.missing_structured_answer(schema.__name__)


def _serialize_answer(answer: BaseModel | Mapping[str, object] | str) -> str:
    if isinstance(answer, BaseModel):
        return answer.model_dump_json()
    if isinstance(answer, str):
        return answer
    return TypeAdapter(Any).dump_json(answer).decode()
