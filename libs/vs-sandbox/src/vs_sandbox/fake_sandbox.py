"""In-memory :class:`~vs_sandbox.execution.Sandbox` test double.

Mirrors :class:`~vs_sandbox.local_shell.LocalShellSandbox`'s observable
contract (an ``id`` property, an ``execute`` method returning
:class:`~vs_sandbox.execution.SandboxExecutionResult`) without a subprocess:
no shell is ever spawned. A caller scripts specific commands with
:meth:`FakeSandbox.script`; anything unscripted falls back to a configurable
default result (a clean success by default), and every call is recorded for
direct assertions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from vs_sandbox.execution import SandboxExecutionResult

#: Result an unscripted command receives when no other default was set.
DEFAULT_RESULT = SandboxExecutionResult(output="", exit_code=0, stdout="", stderr="")

#: Result an empty/invalid command receives, matching ``LocalShellSandbox``.
_INVALID_COMMAND_RESULT = SandboxExecutionResult(
    output="Error: Command must be a non-empty string.", exit_code=1
)


@dataclass(frozen=True, slots=True)
class FakeExecution:
    """One recorded call to :meth:`FakeSandbox.execute`."""

    command: str
    timeout: int | None


@dataclass(slots=True)
class FakeSandbox:
    """Configurable in-memory double for :class:`~vs_sandbox.execution.Sandbox`."""

    _id: str = field(default_factory=lambda: f"fake-{uuid.uuid4().hex[:8]}")
    default_result: SandboxExecutionResult = field(default_factory=lambda: DEFAULT_RESULT)
    calls: list[FakeExecution] = field(default_factory=list)
    _scripted: dict[str, SandboxExecutionResult] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Return this sandbox's identifier."""
        return self._id

    def script(self, command: str, result: SandboxExecutionResult) -> None:
        """Return *result* the next time (and every time) *command* is executed."""
        self._scripted[command] = result

    def execute(self, command: str, *, timeout: int | None = None) -> SandboxExecutionResult:
        """Return the scripted result for *command*, or the default result.

        Matches :meth:`LocalShellSandbox.execute`'s handling of an empty or
        non-string command: it never reaches the script table.
        """
        if not command or not isinstance(command, str):
            return _INVALID_COMMAND_RESULT
        self.calls.append(FakeExecution(command=command, timeout=timeout))
        return self._scripted.get(command, self.default_result)
