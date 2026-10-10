"""Fault-injecting wrappers over agentshim's executor and transport seams.

These are the agentshim-facing halves of ``vs_faults``: the wrappers know how a
long-lived provider process or a provider conversation breaks, and ask a
caller-supplied function which fault, if any, strikes the next position. The
seeded plan that answers it lives in ``vs_faults``; keeping it out of here
keeps ``vs_agent`` free of any fault vocabulary beyond these two enums, and
keeps agentshim imports inside ``vs_agent``.

:class:`FaultingExecutor` hands out the long-lived processes its inner executor
spawns, wrapped so they break the way a host, a container or a provider CLI
breaks: the process dies, goes silent, writes something that is not a protocol
message, or its container is replaced under it. It knows no provider and no
protocol. One-shot ``run`` is not faulted: a single request has no protocol to
break mid-way.

:class:`FaultingTransport` opens the inner transport's conversations wrapped so
a turn fails the way a provider's transport reports it: a classified provider
error, a refused resume, a timeout, a process exit, or a turn that ends in text
that is not the reply. Recovery (retry, a fresh conversation, renewal) is the
session's job, so a faulted turn raises exactly what the real transport would.
A turn runs first and fails afterwards, so the provider did the work whose
reply the caller loses: that is the case a recovery must not replay blindly.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

import agentshim
from agentshim import ProcessExited, StdoutLine

from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_sim.api import Threads


#: What a faulted line of a long-lived agent process's output does instead of
#: arriving. The ordinal counts stdout lines across every process one executor
#: spawned, so a rule names a position in the protocol exchange without
#: knowing the protocol. ``die``: the process is killed before the line is
#: delivered; ``hang``: this line and every later one never arrive; ``malformed``:
#: the line arrives corrupted; ``container_replaced``: every live process dies
#: at once and conversations are lost. The plan vocabulary in ``vs_faults``
#: spells the same four values; a test there keeps the two equal. The kinds are
#: plain strings so a fault plan can be read without importing this package's
#: agentshim-backed API.
type ProcessFaultKind = Literal["die", "hang", "malformed", "container_replaced"]

#: What a faulted turn of a provider conversation does instead of answering:
#: an overload (``transient``), an unclassified failure (``failed``), a refused
#: resume (``resume_refused``), an exceeded budget (``timeout``), a process that
#: exits mid-turn (``exited``), or text that is not the reply (``malformed``).
type ConversationFaultKind = Literal[
    "transient", "failed", "resume_refused", "timeout", "exited", "malformed"
]

type CommandExecutor = agentshim.CommandExecutor
type Transport = agentshim.Transport

#: What a process killed by a fault reports: the exit status of SIGKILL.
KILLED_STATUS = -9
#: What the corrupted line holds; no protocol message starts with it.
MALFORMED_LINE = "}{ truncated frame\n"

TURN_BUDGET_S = 3600.0
"""The turn budget a timed-out turn reports exceeding."""

NOT_A_REPLY = "I could not produce the requested reply."
"""What a malformed turn answers with: prose where a structured reply was due."""


class FaultingExecutor:
    """A ``CommandExecutor`` whose spawned processes fail as ``next_fault`` schedules.

    ``next_fault`` is called once per stdout line, in the order lines are read
    across every process this executor spawned, and returns the fault that
    strikes that line, or ``None``. ``replacements`` counts container
    replacements. ``on_container_replaced`` runs when one fires, so a test can
    make the far end forget what lived in the old container.
    """

    def __init__(
        self,
        inner: CommandExecutor,
        next_fault: Callable[[], ProcessFaultKind | None],
        *,
        on_container_replaced: Callable[[], None] | None = None,
        threads: Threads | None = None,
    ) -> None:
        """Wrap ``inner``; ``next_fault`` decides each line's fate."""
        self._inner = inner
        self._next_fault = next_fault
        self._on_replaced = on_container_replaced
        self._lock = (threads or OsThreads()).lock()
        self._live: list[_FaultyProcess] = []
        self.replacements = 0

    def find_binary(self, name: str, env: Mapping[str, str]) -> str:
        """Delegate binary lookup."""
        return self._inner.find_binary(name, env)

    def check_binary(self, path: str, env: Mapping[str, str], *, timeout: float) -> None:
        """Delegate the health check."""
        self._inner.check_binary(path, env, timeout=timeout)

    def run(
        self, request: agentshim.CommandRequest, sink: agentshim.CommandStreamSink
    ) -> agentshim.CommandResult:
        """Delegate a one-shot command, unfaulted."""
        return self._inner.run(request, sink)

    def spawn(self, request: agentshim.SpawnRequest) -> agentshim.Process:
        """Spawn on the inner executor and wrap the process."""
        process = _FaultyProcess(
            self._inner.spawn(request), self._next_fault, self._replace_container
        )
        with self._lock:
            self._live.append(process)
        return process

    def _replace_container(self) -> None:
        """Kill every live process at once, as replacing their container does."""
        with self._lock:
            victims = list(self._live)
            self._live.clear()
            self.replacements += 1
        for victim in victims:
            victim.die()
        if self._on_replaced is not None:
            self._on_replaced()


class _FaultyProcess:
    """One spawned process, faultable on the lines it writes."""

    def __init__(
        self,
        inner: agentshim.Process,
        next_fault: Callable[[], ProcessFaultKind | None],
        replace_container: Callable[[], None],
    ) -> None:
        self._inner = inner
        self._next_fault = next_fault
        self._replace_container = replace_container
        self._dead = False
        self._hung = False

    def die(self) -> None:
        """End the process; whatever it had not yet delivered is lost."""
        self._dead = True
        self._inner.kill()

    def write(self, data: str) -> None:
        """Write to stdin, or fail as a dead process does."""
        if self._dead:
            message = "the process was killed by an injected fault"
            raise agentshim.ProcessClosedError(message)
        self._inner.write(data)

    def close_stdin(self) -> None:
        """Close stdin."""
        self._inner.close_stdin()

    def next_output(self, timeout: float | None) -> agentshim.ProcessOutput | None:
        """Return the next item, unless a fault kills, silences or corrupts it."""
        if self._dead:
            return ProcessExited(KILLED_STATUS)
        if self._hung:
            # A silent process delivers nothing; stopping it is the caller's call.
            return None
        item = self._inner.next_output(timeout)
        if not isinstance(item, StdoutLine):
            return item
        fault = self._next_fault()
        return item if fault is None else self._apply(fault)

    def _apply(self, fault: ProcessFaultKind) -> agentshim.ProcessOutput | None:
        """What the reader gets in place of the line that ``fault`` strikes."""
        if fault == "hang":
            self._hung = True
            return None
        if fault == "malformed":
            return StdoutLine(MALFORMED_LINE)
        if fault == "container_replaced":
            self._replace_container()
        self.die()
        return ProcessExited(KILLED_STATUS)

    def terminate(self) -> None:
        """Stop the process; a silent one ends too."""
        self._hung = False
        self._inner.terminate()

    def kill(self) -> None:
        """Stop the process now; a silent one ends too."""
        self._hung = False
        self._inner.kill()

    def wait(self, timeout: float | None) -> int | None:
        """Return the exit status once the process has ended."""
        if self._dead:
            return KILLED_STATUS
        return self._inner.wait(timeout)


class FaultingTransport:
    """A ``Transport`` whose conversations fail turns as ``next_fault`` schedules.

    ``next_fault`` is called once per turn, across every conversation this
    transport opened, and returns the fault that strikes it, or ``None``.
    """

    def __init__(
        self, inner: Transport, next_fault: Callable[[], ConversationFaultKind | None]
    ) -> None:
        """Wrap ``inner``; ``next_fault`` decides each turn's fate."""
        self._inner = inner
        self._next_fault = next_fault

    @property
    def profile(self) -> agentshim.ProviderProfile:
        """The inner transport's profile."""
        return self._inner.profile

    def open(self, spec: agentshim.ConversationSpec) -> agentshim.Conversation:
        """Open the inner conversation and wrap it."""
        return _FaultyConversation(self._inner.open(spec), self._next_fault)


class _FaultyConversation:
    """One conversation whose turns the fault source may fail."""

    def __init__(
        self,
        inner: agentshim.Conversation,
        next_fault: Callable[[], ConversationFaultKind | None],
    ) -> None:
        self._inner = inner
        self._next_fault = next_fault

    @property
    def conversation_id(self) -> str | None:
        """The inner conversation's id."""
        return self._inner.conversation_id

    def turn(
        self, request: agentshim.TurnRequest, emit: Callable[[agentshim.AgentEvent], None]
    ) -> agentshim.TurnResult:
        """Run the turn, then lose its reply as the fault source says."""
        fault = self._next_fault()
        result = self._inner.turn(request, emit)
        if fault is None:
            return result
        return _lose(fault, result, self._inner.conversation_id)

    def interrupt(self) -> None:
        """Interrupt the inner conversation's turn."""
        self._inner.interrupt()

    def steer(self, text: str) -> None:
        """Steer the inner conversation, which must be steerable."""
        if not isinstance(self._inner, agentshim.SteerableConversation):
            message = "the wrapped conversation cannot take a message mid-turn"
            raise agentshim.ProviderCapabilityError(message)
        self._inner.steer(text)

    def close(self) -> None:
        """Close the inner conversation."""
        self._inner.close()


def _lose(
    fault: ConversationFaultKind,
    result: agentshim.TurnResult,
    conversation_id: str | None,
) -> agentshim.TurnResult:
    """Return the failure (or the corrupted result) ``fault`` makes of a finished turn."""
    argv = ("agent",)
    if fault == "malformed":
        return replace(result, text=NOT_A_REPLY, structured_output=None)
    if fault == "timeout":
        raise agentshim.TurnTimeoutError(TURN_BUDGET_S)
    if fault == "resume_refused":
        raise agentshim.SessionResumeError(argv, 1, conversation_id or "unknown")
    if fault == "exited":
        raise agentshim.CliExitError(argv, -9, stderr="killed")
    kind = agentshim.FailureKind.TRANSIENT if fault == "transient" else agentshim.FailureKind.OTHER
    message = f"injected {fault} turn failure"
    raise agentshim.TurnFailedError(message, kind=kind)
