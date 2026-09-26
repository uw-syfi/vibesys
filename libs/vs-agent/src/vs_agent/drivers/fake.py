"""Fake implementation of the stateful agent-driver contract.

The fake exists so a run can be exercised end to end without an agent CLI,
a model, or a network. It is a driver, not a shortcut around one: every event
it produces leaves through the ``AgentObserver`` its caller passed to
:meth:`FakeSession.run_turn`, which means it reaches the output sink, the
core event journal, and any composed adapters by exactly the same route a real
driver's events take. The fake never writes an event, a state file, or a log itself.
Anything it wrote directly would be a path integration tests then stop
covering.

A caller scripts a turn explicitly, as a list of :class:`~vs_agent.contracts.
AgentEvent` values built with the module-level helpers below
(:func:`assistant_text`, :func:`thinking`, :func:`tool_call`,
:func:`tool_result`, :func:`todo_write`, :func:`usage`), rather than through
count knobs: what a test asserts on is what it wrote.

Structured turns answer with :func:`~vs_agent.scripted_rounds.
scripted_round_payload` unless the caller supplies its own ``answer``, so a
scripted run completes loop rounds on the happy path even when a test only
cares about the event stream.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

from vs_agent.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
)
from vs_agent.events import CommandResultPayload
from vs_agent.scripted_rounds import round_number_from_label, scripted_round_payload

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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
    def driver_closed(cls) -> FakeDriverError:
        """Describe session creation after the fake driver was closed."""
        return cls("fake agent driver is closed")

    @classmethod
    def missing_scripted_artifact(cls, schema_name: str) -> FakeDriverError:
        """Describe an output schema without a corresponding scripted artifact."""
        return cls(
            f"no scripted artifact for response schema {schema_name!r}; add one to "
            "vs_agent.scripted_rounds so scripted runs keep covering this role"
        )


class FakeSession:
    """One fake conversation. Its state is which scripted turn runs next."""

    def __init__(
        self,
        *,
        spec: AgentSessionSpec,
        turns: tuple[tuple[AgentEvent, ...], ...],
        answer: BaseModel | Mapping[str, object] | str | None,
    ) -> None:
        """Create a session bound to ``turns``/``answer`` for ``spec``'s role."""
        self._spec = spec
        self._turns = turns
        self._answer = answer
        self._invocations = 0
        self._closed = False
        self._resumed_session_id: str | None = None

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Emit the next scripted turn's events, then answer it."""
        if self._closed:
            raise FakeDriverError.session_closed()
        self._invocations += 1
        # A labelled turn keeps the loop's own round numbering, so scripted
        # artifacts line up with the round the caller thinks it is running.
        # An unlabelled turn falls back to this session's invocation count.
        round_index = round_number_from_label(request.label) if request.label else self._invocations
        events = self._turns[min(self._invocations, len(self._turns)) - 1]
        for event in events:
            if observer is not None:
                observer.on_event(event)
        return AgentTurnResult(
            text=_turn_text(self._answer, request, round_index),
            usage=_turn_usage(events),
            # An adopted session ID is echoed back so a resumed run's continuity
            # is observable; otherwise each session mints its own stable ID.
            provider_session_id=(
                self._resumed_session_id or f"fake-{self._spec.role}-{id(self):x}"
            ),
        )

    def cancel(self) -> None:
        """Do nothing: a fake turn is synchronous and always already finished.

        ``run_turn`` emits its scripted events on the calling thread and
        returns, so there is never an in-flight turn for another thread to
        stop. Kept so the fake satisfies the whole session contract.
        """

    def close(self) -> None:
        """Release this session. Idempotent; the fake owns no resources."""
        self._closed = True

    def resume_provider_session(self, session_id: str) -> bool:
        """Adopt ``session_id`` so a resumed run's continuity is observable."""
        self._resumed_session_id = session_id
        return True


class FakeDriver:
    """Create fake sessions that stream scripted turns instead of running an agent."""

    def __init__(
        self,
        *,
        turn: Sequence[AgentEvent] | None = None,
        turns: Sequence[Sequence[AgentEvent]] | None = None,
        answer: BaseModel | Mapping[str, object] | str | None = None,
    ) -> None:
        """Create a driver whose sessions emit ``turn``/``turns`` and answer with ``answer``.

        ``turn`` is the event sequence emitted for every turn a session runs.
        ``turns`` is a sequence of distinct per-turn event sequences: a
        session's Nth ``run_turn`` call emits ``turns[N - 1]``, and once a
        session has run more turns than ``turns`` has entries, its last entry
        keeps repeating. ``turn=[...]`` is sugar for ``turns=[[...]]``.
        Passing neither runs a turn that emits no events. ``answer`` sets the
        text or structured payload every turn returns; when omitted, a
        structured turn falls back to
        :func:`~vs_agent.scripted_rounds.scripted_round_payload` for the
        requested response schema and round, so tests that only assert on the
        event stream need not pass one.
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
        self._turns = resolved_turns
        self._answer = answer
        self._sessions: list[FakeSession] = []
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe what the fake honors; see :data:`FAKE_CAPABILITIES`."""
        return FAKE_CAPABILITIES

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create one fake conversation for ``spec``."""
        if self._closed:
            raise FakeDriverError.driver_closed()
        session = FakeSession(spec=spec, turns=self._turns, answer=self._answer)
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
    round_index: int,
) -> str:
    if answer is not None:
        return _serialize_answer(answer)
    schema = request.output_schema
    if schema is None:
        return f"Fake agent completed {request.label or 'a turn'} with no workspace changes."
    payload = scripted_round_payload(schema.__name__, round_index)
    if payload is None:
        raise FakeDriverError.missing_scripted_artifact(schema.__name__)
    return json.dumps(payload)


def _serialize_answer(answer: BaseModel | Mapping[str, object] | str) -> str:
    if isinstance(answer, BaseModel):
        return answer.model_dump_json()
    if isinstance(answer, str):
        return answer
    return json.dumps(answer)
