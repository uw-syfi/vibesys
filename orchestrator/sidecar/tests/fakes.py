"""A scripted ``Driver``: events a test writes down, played back turn by turn.

It stands where agentshim's session stands, for tests about the engine and the wire that do not
need a provider's frames. Provider frames are covered by the tests that use agentshim's scripted
providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_sidecar.driver import Opened, SteerRefusedError, TurnResult
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshim import AgentEvent


class CrashError(BaseException):
    """The sidecar process dies here."""


@dataclass
class TurnScript:
    """What the provider does in one turn."""

    events: Sequence[AgentEvent | type[CrashError]] = ()
    status: int = orch.TURN_STATUS_COMPLETED
    #: Events played when a steer arrives (it is consumed), none when the turn ignores steers.
    on_steer: Sequence[AgentEvent] = ()


@dataclass
class ScriptedDriver:
    """A ``Driver`` that plays ``turns`` in order, one per ``run_turn``."""

    turns: list[TurnScript] = field(default_factory=list)
    conversation_id: str = "conversation-1"
    idle: list[Sequence[AgentEvent]] = field(default_factory=list)
    alive: bool = True
    prompts: list[str] = field(default_factory=list)
    steers: list[str] = field(default_factory=list)
    interrupts: int = 0
    closed: bool = False
    _emit: Callable[[AgentEvent], None] | None = None
    _running: TurnScript | None = None

    def open(self) -> Opened:
        """See ``Driver.open``."""
        return Opened(
            self.conversation_id, orch.Capabilities(streams_text=True, steers=True, resumes=True)
        )

    def run_turn(self, prompt: str, emit: Callable[[AgentEvent], None]) -> TurnResult:
        """See ``Driver.run_turn``."""
        self.prompts.append(prompt)
        script = self.turns.pop(0)
        self._emit, self._running = emit, script
        try:
            for event in script.events:
                if event is CrashError:
                    raise CrashError
                emit(event)  # type: ignore[arg-type]
        finally:
            self._emit, self._running = None, None
        return TurnResult(script.status)

    def steer(self, text: str) -> None:
        """See ``Driver.steer``."""
        if self._running is None or not self._running.on_steer or self._emit is None:
            msg = "the turn does not take steers"
            raise SteerRefusedError(msg)
        self.steers.append(text)
        for event in self._running.on_steer:
            self._emit(event)

    def interrupt(self) -> None:
        """See ``Driver.interrupt``."""
        self.interrupts += 1

    def poll_idle(self, emit: Callable[[AgentEvent], None]) -> bool:
        """See ``Driver.poll_idle``."""
        for event in self.idle.pop(0) if self.idle else ():
            emit(event)
        return self.alive

    def close(self) -> None:
        """See ``Driver.close``."""
        self.closed = True
