"""In-memory Docker command runner for sandbox composition tests."""

from __future__ import annotations

import subprocess
from collections import defaultdict, deque
from dataclasses import dataclass

type DockerCommandOutcome = (
    subprocess.CompletedProcess[str] | FileNotFoundError | subprocess.TimeoutExpired
)


@dataclass(frozen=True, slots=True)
class DockerCommandCall:
    """One Docker CLI invocation observed by the fake runner."""

    arguments: tuple[str, ...]
    timeout_seconds: int


class FakeDockerCommandRunner:
    """Run command-specific scripted outcomes without starting a process."""

    def __init__(self) -> None:
        """Start with an empty command script and call history."""
        self.calls: list[DockerCommandCall] = []
        self._outcomes: dict[tuple[str, ...], deque[DockerCommandOutcome]] = defaultdict(deque)

    def script(
        self,
        arguments: tuple[str, ...],
        *outcomes: DockerCommandOutcome,
    ) -> None:
        """Append ordered outcomes for one exact shell-free argv."""
        if not outcomes:
            message = "a scripted Docker command requires at least one outcome"
            raise ValueError(message)
        self._outcomes[arguments].extend(outcomes)

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        """Return or raise the next outcome for *arguments*."""
        self.calls.append(DockerCommandCall(arguments=arguments, timeout_seconds=timeout_seconds))
        outcomes = self._outcomes[arguments]
        if not outcomes:
            message = f"no scripted Docker command outcome for {arguments!r}"
            raise AssertionError(message)
        outcome = outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
