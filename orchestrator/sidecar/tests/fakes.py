"""Fakes for the sidecar's tests: a scripted ``Driver`` and files that fail where a test says.

``ScriptedDriver`` stands where agentshim's session stands, for tests about the engine and the wire
that do not need a provider's frames; provider frames are covered by the tests that use agentshim's
scripted providers. It can pause the engine at the two moments a command races a turn: after the
turn is announced and before the provider starts it (``at_start``), and after the provider is done
and before the engine has noticed (``at_end``). The hooks run on the calling thread, so a test
sends its command from the hook and nothing depends on timing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_sidecar.driver import Opened, StartError, SteerRefusedError, TurnResult
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshim import AgentEvent, ProviderUsage


class CrashError(BaseException):
    """The sidecar process dies here."""


@dataclass
class TurnScript:
    """What the provider does in one turn."""

    events: Sequence[AgentEvent | type[CrashError] | Callable[[], None]] = ()
    status: int = orch.TURN_STATUS_COMPLETED
    usage: ProviderUsage | None = None
    #: Events played when a steer arrives (it is consumed), none when the turn ignores steers.
    on_steer: Sequence[AgentEvent] = ()


@dataclass
class ScriptedDriver:
    """A ``Driver`` that plays ``turns`` in order, one per ``run_turn``.

    Like the real driver, it routes ``steer`` and ``interrupt`` by turn id: one for a turn that is
    not the current one is ignored (interrupt) or refused (steer), and an interrupt for the
    current turn, even before ``run_turn`` has played anything, ends it ``INTERRUPTED``.
    """

    turns: list[TurnScript] = field(default_factory=list)
    conversation_id: str = "conversation-1"
    idle: list[Sequence[AgentEvent]] = field(default_factory=list)
    alive: bool = True
    prompts: list[str] = field(default_factory=list)
    steers: list[tuple[str, str]] = field(default_factory=list)
    interrupts: list[str] = field(default_factory=list)
    ignored_interrupts: list[str] = field(default_factory=list)
    closed: bool = False
    #: When set, ``open`` raises it: the session cannot start.
    start_error: StartError | None = None
    #: Called when the turn has begun and the provider has not started it.
    at_start: Callable[[], None] | None = None
    #: Called when the provider is done with the turn and the engine does not know yet.
    at_end: Callable[[], None] | None = None
    _emit: Callable[[AgentEvent], None] | None = None
    _running: TurnScript | None = None
    _current: str | None = None
    _stopped: bool = False

    def open(self) -> Opened:
        """See ``Driver.open``."""
        if self.start_error is not None:
            raise self.start_error
        return Opened(
            self.conversation_id, orch.Capabilities(streams_text=True, steers=True, resumes=True)
        )

    def begin_turn(self, turn_id: str, prompt: str) -> None:
        """See ``Driver.begin_turn``."""
        self._current, self._stopped = turn_id, False
        self.prompts.append(prompt)

    def run_turn(self, emit: Callable[[AgentEvent], None]) -> TurnResult:
        """See ``Driver.run_turn``."""
        script = self.turns.pop(0)
        if self.at_start is not None:
            self.at_start()
        if self._stopped:
            self._current = None
            if self.at_end is not None:
                self.at_end()
            return TurnResult(orch.TURN_STATUS_INTERRUPTED)
        self._emit, self._running = emit, script
        status = script.status
        try:
            for event in script.events:
                if event is CrashError:
                    raise CrashError
                if callable(event):
                    event()
                else:
                    emit(event)  # type: ignore[arg-type]
                if self._stopped:
                    status = orch.TURN_STATUS_INTERRUPTED
                    break
        finally:
            self._emit, self._running = None, None
        self._current = None
        if self.at_end is not None:
            self.at_end()
        return TurnResult(status, script.usage)

    def steer(self, turn_id: str, text: str) -> None:
        """See ``Driver.steer``."""
        if (
            turn_id != self._current
            or self._running is None
            or not self._running.on_steer
            or self._emit is None
        ):
            msg = "the turn does not take steers"
            raise SteerRefusedError(msg)
        self.steers.append((turn_id, text))
        for event in self._running.on_steer:
            self._emit(event)

    def interrupt(self, turn_id: str) -> None:
        """See ``Driver.interrupt``."""
        if turn_id != self._current:
            self.ignored_interrupts.append(turn_id)
            return
        self.interrupts.append(turn_id)
        self._stopped = True

    def poll_idle(self, emit: Callable[[AgentEvent], None]) -> bool:
        """See ``Driver.poll_idle``."""
        for event in self.idle.pop(0) if self.idle else ():
            emit(event)
        return self.alive

    def close(self) -> None:
        """See ``Driver.close``."""
        self.closed = True


class FileFaultError(OSError):
    """A write the test told ``FaultyFiles`` to fail."""


@dataclass
class FaultyFiles:
    """In-memory ``Files`` whose Nth append (counted over all files) fails or crashes.

    ``fail_at`` / ``crash_at`` are 1-based append numbers; the append writes its first ``keep``
    bytes and then raises ``FileFaultError`` (a failed write) or ``CrashError`` (the process
    dies). ``fail_truncate`` makes truncation fail too, as a disk that is gone does.
    """

    fail_at: int | None = None
    crash_at: int | None = None
    keep: int = 0
    fail_truncate: bool = False
    data: dict[str, bytearray] = field(default_factory=dict)
    appends: int = 0
    durable_appends: int = 0

    def open(self, name: str) -> _FaultyLog:
        """See ``Files.open``."""
        return _FaultyLog(self, self.data.setdefault(name, bytearray()))

    def reopened(self) -> FaultyFiles:
        """The same bytes, as a restarted process would find them, with no fault planned."""
        return FaultyFiles(data={name: bytearray(data) for name, data in self.data.items()})


@dataclass
class _FaultyLog:
    owner: FaultyFiles
    data: bytearray

    def read(self) -> bytes:
        return bytes(self.data)

    def append(self, data: bytes, *, durable: bool) -> None:
        self.owner.appends += 1
        self.owner.durable_appends += durable
        if self.owner.appends in (self.owner.fail_at, self.owner.crash_at):
            self.data += data[: self.owner.keep]
            raise (CrashError if self.owner.appends == self.owner.crash_at else FileFaultError)
        self.data += data

    def truncate(self, size: int) -> None:
        if len(self.data) == size:
            return
        if self.owner.fail_truncate:
            msg = "the disk is gone"
            raise FileFaultError(msg)
        del self.data[size:]
