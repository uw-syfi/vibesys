"""Mock implementation of the stateful agent-driver contract.

The mock exists so a run can be exercised end to end without an agent CLI,
a model, or a network. It is a driver, not a shortcut around one: every event
it produces leaves through the ``AgentObserver`` its caller passed to
:meth:`MockSession.run_turn`, which means it reaches the output sink, the
core event journal, and any composed adapters by exactly the same route a real
driver's events take. The mock never writes an event, a state file, or a log itself.
Anything it wrote directly would be a path integration tests then stop
covering.

Two playbooks:

``ScriptedPlaybook``
    Synthesize a deterministic turn: assistant text chunks, tool call/result
    pairs of a configured size, a todo snapshot, and a usage update. Volume
    and pacing are knobs, so the same driver serves a fast unit test and a
    deliberately event-heavy boot fixture.

``ReplayPlaybook``
    Re-emit the events of a recorded run's ``run-events.jsonl``, optionally
    honoring the recorded inter-event gaps. Useful when a synthetic stream is
    not representative enough of what a real run produced.

Both playbooks answer a structured turn with
:func:`~vibesys.agents.scripted_rounds.scripted_round_payload`, so a scripted
run completes loop rounds on the happy path. A replay reproduces the recorded
*event stream*; its turn result still comes from the scripted artifacts,
because a recorded event log does not carry the agent's structured answer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
)
from vibesys.agents.scripted_rounds import round_number_from_label, scripted_round_payload
from vibesys.agents.todos import todos_from_tool_call
from vibesys.run.events import (
    AgentOutputChunkData,
    CommandResultPayload,
    CoreEvent,
    CoreEventType,
    EventPayload,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from vibesys.agents.contracts import AgentObserver

MOCK_CAPABILITIES = AgentCapabilities(
    mcp_servers=True,
    in_process_tools=False,
    nested_read_only_paths=True,
    hidden_paths=True,
    host_path_grants=True,
    container_execution=True,
    timeouts=True,
    session_reuse=True,
    provider_session_resume=True,
)
"""What the mock can honor without weakening the semantics it was handed.

The mock starts no process and touches no path outside its own playbook, so
every sandbox restriction in an ``AgentExecutionPolicy`` is satisfied
vacuously: there is nothing running that could reach a restricted path. The
mock therefore accepts any policy rather than rejecting configurations a real
driver would have run.
"""

# The todo tool whose call the callback layer turns into a todo snapshot.
# Emitting the tool call (rather than a todo event) is the point: the mock
# exercises the same provider-vocabulary translation a real driver does.
_TODO_TOOL = "TodoWrite"

_REPLAYABLE_EVENT_TYPES = frozenset(
    {
        CoreEventType.AGENT_OUTPUT_CHUNK,
        CoreEventType.TOOL_CALL,
        CoreEventType.TOOL_RESULT,
        CoreEventType.TODO_UPDATE,
        CoreEventType.USAGE_UPDATE,
    }
)

_REPLAYABLE_OUTPUT_CHANNELS: dict[str, AgentEventKind] = {
    "assistant": AgentEventKind.TEXT,
    "analysis": AgentEventKind.THINKING,
}


class MockDriverError(RuntimeError):
    """A mock playbook was configured with something it cannot run."""


@dataclass(frozen=True, slots=True)
class ScriptedPlaybook:
    """Event volume and pacing for one synthesized turn.

    Defaults are small and instantaneous so an ordinary test pays nothing.
    Raise ``rounds``/``tool_calls``/``text_chunks`` to build the event-heavy
    histories that boot-path tests need.
    """

    text_chunks: int = 4
    text_chunk_chars: int = 48
    thinking_chunks: int = 1
    tool_calls: int = 2
    tool_arg_chars: int = 64
    tool_result_chars: int = 256
    todo_updates: int = 1
    todo_items: int = 3
    usage_updates: int = 1
    step_delay_seconds: float = 0.0
    """Sleep between emitted events. Zero (the default) is as fast as possible."""

    def __post_init__(self) -> None:
        """Reject a playbook whose counts cannot describe a turn."""
        negative = {
            name: value
            for name, value in (
                ("text_chunks", self.text_chunks),
                ("text_chunk_chars", self.text_chunk_chars),
                ("thinking_chunks", self.thinking_chunks),
                ("tool_calls", self.tool_calls),
                ("tool_arg_chars", self.tool_arg_chars),
                ("tool_result_chars", self.tool_result_chars),
                ("todo_updates", self.todo_updates),
                ("todo_items", self.todo_items),
                ("usage_updates", self.usage_updates),
                ("step_delay_seconds", self.step_delay_seconds),
            )
            if value < 0
        }
        if negative:
            raise MockDriverError(  # noqa: TRY003  # tracked: #288
                f"ScriptedPlaybook fields must be non-negative: {negative}"
            )


@dataclass(frozen=True, slots=True)
class ReplayPlaybook:
    """Re-emit one recorded run's events through the driver contract.

    ``speed`` multiplies the recorded inter-event gaps: ``0`` (the default)
    drops the gaps entirely and replays as fast as the consumer accepts
    events, ``1`` reproduces the original pacing, ``2`` runs twice as fast.
    """

    events_path: Path
    speed: float = 0.0
    max_gap_seconds: float = 1.0
    """Cap on any single reproduced gap, so one idle stretch cannot stall a test."""

    def __post_init__(self) -> None:
        """Reject a negative speed, which has no meaning for a replay."""
        if self.speed < 0:
            raise MockDriverError(  # noqa: TRY003  # tracked: #288
                f"ReplayPlaybook speed must be non-negative, got {self.speed}"
            )


Playbook = ScriptedPlaybook | ReplayPlaybook


class MockSession:
    """One mock conversation. Its only state is how many turns it has run."""

    def __init__(self, *, spec: AgentSessionSpec, playbook: Playbook) -> None:
        """Create a session bound to ``playbook`` for ``spec``'s role."""
        self._spec = spec
        self._playbook = playbook
        self._turns = 0
        self._closed = False
        self._resumed_session_id: str | None = None

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Emit the playbook's events, then answer with a scripted artifact."""
        if self._closed:
            raise MockDriverError("mock agent session is closed")  # noqa: TRY003  # tracked: #288
        self._turns += 1
        # A labelled turn keeps the loop's own round numbering, so scripted
        # artifacts line up with the round the caller thinks it is running.
        # An unlabelled turn falls back to this session's turn count.
        round_index = round_number_from_label(request.label) if request.label else self._turns
        events = (
            _scripted_events(self._playbook, self._spec, round_index)
            if isinstance(self._playbook, ScriptedPlaybook)
            else _replayed_events(self._playbook)
        )
        for event in events:
            if observer is not None:
                observer.on_event(event)
        return AgentTurnResult(
            text=_scripted_turn_text(request, round_index),
            usage=_scripted_usage(round_index),
            # An adopted session ID is echoed back so a resumed run's continuity
            # is observable; otherwise each session mints its own stable ID.
            provider_session_id=(
                self._resumed_session_id or f"mock-{self._spec.role}-{id(self):x}"
            ),
        )

    def cancel(self) -> None:
        """Do nothing: a mock turn is synchronous and always already finished.

        ``run_turn`` emits its scripted events on the calling thread and
        returns, so there is never an in-flight turn for another thread to
        stop. Kept so the mock satisfies the whole session contract.
        """

    def close(self) -> None:
        """Release this session. Idempotent; the mock owns no resources."""
        self._closed = True

    def resume_provider_session(self, session_id: str) -> bool:
        """Adopt ``session_id`` so a resumed run's continuity is observable."""
        self._resumed_session_id = session_id
        return True


class MockDriver:
    """Create mock sessions that stream a playbook instead of running an agent."""

    def __init__(self, playbook: Playbook | None = None) -> None:
        """Create a driver whose sessions all run ``playbook``."""
        self._playbook: Playbook = playbook if playbook is not None else ScriptedPlaybook()
        self._sessions: list[MockSession] = []
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe what the mock honors; see :data:`MOCK_CAPABILITIES`."""
        return MOCK_CAPABILITIES

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create one mock conversation for ``spec``."""
        if self._closed:
            raise MockDriverError("mock agent driver is closed")  # noqa: TRY003  # tracked: #288
        session = MockSession(spec=spec, playbook=self._playbook)
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


def supported_providers() -> list[str]:
    """Return the provider names the mock driver accepts.

    The mock drives nothing, so provider selection only labels the run.
    """
    return ["mock"]


# --- scripted mode ---------------------------------------------------------


def _scripted_events(
    playbook: ScriptedPlaybook, spec: AgentSessionSpec, round_index: int
) -> Iterator[AgentEvent]:
    for index in range(playbook.thinking_chunks):
        yield from _paced(
            playbook,
            AgentEvent(
                kind=AgentEventKind.THINKING,
                text=f"[mock:{spec.role}] round {round_index} reasoning step {index + 1}\n",
            ),
        )
    for index in range(playbook.text_chunks):
        yield from _paced(
            playbook,
            AgentEvent(
                kind=AgentEventKind.TEXT,
                text=_filler(f"r{round_index}c{index}", playbook.text_chunk_chars),
            ),
        )
    for index in range(playbook.todo_updates):
        yield from _paced(playbook, _todo_event(playbook, round_index, index))
    for index in range(playbook.tool_calls):
        yield from _paced(playbook, _tool_call_event(playbook, round_index, index))
        yield from _paced(playbook, _tool_result_event(playbook, round_index, index))
    for _ in range(playbook.usage_updates):
        yield from _paced(
            playbook,
            AgentEvent(kind=AgentEventKind.USAGE, usage=_scripted_usage(round_index)),
        )


def _paced(playbook: ScriptedPlaybook, event: AgentEvent) -> Iterator[AgentEvent]:
    if playbook.step_delay_seconds:
        time.sleep(playbook.step_delay_seconds)
    yield event


def _todo_event(playbook: ScriptedPlaybook, round_index: int, index: int) -> AgentEvent:
    """A todo snapshot delivered the way a provider delivers one: a tool call.

    Todos reach the sink through ``todos_from_tool_call``, so emitting the
    tool call keeps the mock on the same translation path as a real driver.
    """
    todos = [
        {
            "content": f"round {round_index} step {item + 1}",
            "status": _todo_status(item, index),
        }
        for item in range(playbook.todo_items)
    ]
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        payload={"tool": _TODO_TOOL, "args": {"todos": todos}},
    )


def _todo_status(item: int, update_index: int) -> str:
    if item < update_index:
        return "completed"
    return "in_progress" if item == update_index else "pending"


def _tool_call_event(playbook: ScriptedPlaybook, round_index: int, index: int) -> AgentEvent:
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        payload={
            "tool": "Bash",
            "args": {
                "command": _filler(f"cmd-r{round_index}-{index}", playbook.tool_arg_chars),
                "description": f"mock tool call {index + 1}",
            },
        },
    )


def _tool_result_event(playbook: ScriptedPlaybook, round_index: int, index: int) -> AgentEvent:
    stdout = _filler(f"out-r{round_index}-{index}", playbook.tool_result_chars)
    return AgentEvent(
        kind=AgentEventKind.TOOL_RESULT,
        text=stdout,
        payload={
            "tool": "Bash",
            "stdout": stdout,
            "stderr": "",
            "exit_code": 0,
            "duration": 0.0,
            "result_payload": CommandResultPayload(
                stdout=stdout, stderr="", exit_code=0, duration=0.0
            ),
        },
    )


def _filler(seed: str, size: int) -> str:
    """Return exactly ``size`` deterministic characters tagged with ``seed``."""
    if size <= 0:
        return ""
    prefix = f"{seed}:"
    if len(prefix) >= size:
        return prefix[:size]
    body = "abcdefghijklmnopqrstuvwxyz0123456789 "
    fill_length = size - len(prefix)
    repeats = fill_length // len(body) + 1
    return prefix + (body * repeats)[:fill_length]


def _scripted_usage(round_index: int) -> AgentUsage:
    return AgentUsage(
        input_tokens=1000 + 250 * round_index,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=500,
        output_tokens=120,
        total_cost_usd=0.0,
        duration_ms=10,
    )


def _scripted_turn_text(request: AgentTurnRequest, round_index: int) -> str:
    schema = request.output_schema
    if schema is None:
        return f"Mock agent completed {request.label or 'a turn'} with no workspace changes."
    payload = scripted_round_payload(schema.__name__, round_index)
    if payload is None:
        raise MockDriverError(  # noqa: TRY003  # tracked: #288
            f"no scripted artifact for response schema {schema.__name__!r}; add one to "
            "vibesys.agents.scripted_rounds so scripted runs keep covering this role"
        )
    return json.dumps(payload)


# --- replay mode -----------------------------------------------------------


def _replayed_events(playbook: ReplayPlaybook) -> Iterator[AgentEvent]:
    previous: float | None = None
    todos_already_carried = False
    for event in _recorded_events(playbook.events_path):
        # A recorded todo snapshot that a recorded tool call already carries
        # is derived state: re-emitting both would duplicate it downstream,
        # because the tool call regenerates the snapshot on its own.
        if event.type is CoreEventType.TODO_UPDATE and todos_already_carried:
            todos_already_carried = False
            continue
        driver_event = _as_driver_event(event)
        if driver_event is None:
            continue
        todos_already_carried = _carries_todos(driver_event)
        recorded = event.timestamp.timestamp()
        if playbook.speed > 0 and previous is not None:
            gap = min((recorded - previous) / playbook.speed, playbook.max_gap_seconds)
            if gap > 0:
                time.sleep(gap)
        previous = recorded
        yield driver_event


def _carries_todos(event: AgentEvent) -> bool:
    """Whether downstream will derive a todo snapshot from this driver event."""
    if event.kind is not AgentEventKind.TOOL_CALL:
        return False
    args = event.payload.get("args")
    if not isinstance(args, dict):
        return False
    return todos_from_tool_call(str(event.payload.get("tool", "")), args) is not None


def _recorded_events(events_path: Path) -> Iterator[CoreEvent]:
    """Read one recorded run log, skipping records this fixture cannot use.

    A recording is test input, not a live contract: a truncated final line
    (the usual shape of a log captured from a killed run) is skipped rather
    than failing the replay.
    """
    if not events_path.is_file():
        raise MockDriverError(f"replay event log not found: {events_path}")  # noqa: TRY003  # tracked: #288
    with events_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                event_type = CoreEventType(raw["type"])
                if event_type not in _REPLAYABLE_EVENT_TYPES:
                    continue
                yield CoreEvent.model_validate(
                    {
                        "sequence": raw.get("sequence", 0),
                        "run_id": raw.get("run_id", ""),
                        "timestamp": raw["timestamp"],
                        "type": event_type,
                        "text": raw.get("text", ""),
                        "status": raw.get("status"),
                        "round_label": raw.get("round_label"),
                        "agent_kind": raw.get("agent_kind"),
                        "execution_id": raw.get("execution_id") or raw.get("invocation_id"),
                        "data": raw.get("data"),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue


def _as_driver_event(event: CoreEvent) -> AgentEvent | None:
    """Project one recorded run event back onto the driver's event vocabulary."""
    if event.type not in _REPLAYABLE_EVENT_TYPES:
        return None
    data = event.data
    for payload_type, convert in _REPLAY_CONVERTERS:
        if isinstance(data, payload_type):
            return convert(data)
    return None


def _replay_output_chunk(data: AgentOutputChunkData) -> AgentEvent | None:
    """Only two channels come from a driver.

    ``diagnostic``, ``prompt``, and ``tool`` chunks in a recording were
    written by the client and runner above the driver; replaying them would
    push the client's own narration back through the driver contract.
    """
    kind = _REPLAYABLE_OUTPUT_CHANNELS.get(data.channel)
    return None if kind is None else AgentEvent(kind=kind, text=data.content)


def _replay_tool_call(data: ToolCallData) -> AgentEvent:
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        payload={"tool": data.tool, "args": dict(data.args)},
    )


def _replay_todo_update(data: TodoUpdateData) -> AgentEvent:
    """Re-emit a recorded todo snapshot as the provider tool call behind it.

    The driver contract has no todo event; snapshots are derived downstream
    from a plan tool call, so that is the form replay has to use.
    """
    todos: list[dict[str, Any]] = [
        {"content": todo.content, "status": todo.status} for todo in data.todos
    ]
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        payload={"tool": _TODO_TOOL, "args": {"todos": todos}},
    )


def _replay_tool_result(data: ToolResultData) -> AgentEvent:
    stdout = "" if data.is_error else data.content
    stderr = data.content if data.is_error else ""
    return AgentEvent(
        kind=AgentEventKind.TOOL_RESULT,
        text=data.content,
        payload={
            "tool": data.tool,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": 1 if data.is_error else 0,
            "duration": None,
            "result_payload": data.payload,
        },
    )


def _replay_usage_update(data: UsageUpdateData) -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.USAGE, usage=AgentUsage(input_tokens=data.input_tokens))


_REPLAY_CONVERTERS: tuple[tuple[type[EventPayload], Callable[[Any], AgentEvent | None]], ...] = (
    (AgentOutputChunkData, _replay_output_chunk),
    (ToolCallData, _replay_tool_call),
    (TodoUpdateData, _replay_todo_update),
    (ToolResultData, _replay_tool_result),
    (UsageUpdateData, _replay_usage_update),
)
"""Recorded payload type to the driver event it was projected from."""
