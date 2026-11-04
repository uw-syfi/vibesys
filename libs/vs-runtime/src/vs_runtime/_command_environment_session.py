"""Role-specific command runners paired with one owned environment session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sandbox.api import CaptureCommandRunner, CommandRunner, SandboxSession


@dataclass(slots=True)
class CommandRunEnvironmentSession[ViewT]:
    """Delegate lifecycle and ordinary commands while selecting capture commands."""

    delegate: SandboxSession[ViewT]
    capture_commands: CaptureCommandRunner

    @property
    def sandbox(self) -> CommandRunner:
        return self.delegate.sandbox

    @sandbox.setter
    def sandbox(self, value: CommandRunner) -> None:
        self.delegate.sandbox = value

    @property
    def view(self) -> ViewT:
        return self.delegate.view

    @view.setter
    def view(self, value: ViewT) -> None:
        self.delegate.view = value

    def __enter__(self) -> CommandRunEnvironmentSession[ViewT]:
        self.delegate.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.delegate.__exit__(exc_type, exc, tb)

    def close(self) -> None:
        """Release the owned editor session."""
        self.delegate.close()


def command_environment_session[ViewT](
    session: SandboxSession[ViewT],
    capture_commands: CaptureCommandRunner | None = None,
) -> CommandRunEnvironmentSession[ViewT]:
    """Pair an editor session with its explicit role-specific capture runner."""
    selected = session.sandbox if capture_commands is None else capture_commands
    return CommandRunEnvironmentSession(session, selected)


__all__ = [
    "CommandRunEnvironmentSession",
    "command_environment_session",
]
