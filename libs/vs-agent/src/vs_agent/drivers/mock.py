"""Mock implementation of the stateful agent-driver contract.

The mock exists so a run can be exercised end to end without an agent CLI,
a model, or a network. It is a driver, not a shortcut around one: every event
it produces leaves through the ``AgentObserver`` its caller passed to
:meth:`MockSession.run_turn`, which means it reaches the output sink, the
core event journal, and any composed adapters by exactly the same route a real
driver's events take. The mock never writes an event, a state file, or a log itself.
Anything it wrote directly would be a path integration tests then stop
covering.

``ScriptedPlaybook``
    Synthesize a deterministic turn: assistant text chunks, tool call/result
    pairs of a configured size, a todo snapshot, and a usage update. Volume
    and pacing are knobs, so the same driver serves a fast unit test and a
    deliberately event-heavy boot fixture.

The playbook answers a structured turn with
:func:`~vs_agent.scripted_rounds.scripted_round_payload`, so a scripted
run completes loop rounds on the happy path.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    from collections.abc import Iterator

    from vs_agent.contracts import AgentObserver

MOCK_CAPABILITIES = AgentCapabilities(
    mcp_servers=True,
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


class MockSession:
    """One mock conversation. Its only state is how many turns it has run."""

    def __init__(self, *, spec: AgentSessionSpec, playbook: ScriptedPlaybook) -> None:
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
        events = _scripted_events(self._playbook, self._spec, round_index)
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

    def __init__(self, playbook: ScriptedPlaybook | None = None) -> None:
        """Create a driver whose sessions all run ``playbook``."""
        self._playbook: ScriptedPlaybook = playbook if playbook is not None else ScriptedPlaybook()
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
            "vs_agent.scripted_rounds so scripted runs keep covering this role"
        )
    return json.dumps(payload)
