"""In-memory Docker command runner and CLI for sandbox composition tests."""

from __future__ import annotations

import subprocess
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

# Tests build Docker outcomes through the helpers below, so they never import
# ``subprocess`` just to describe what a scripted ``docker`` printed or how it failed.
DockerResult = subprocess.CompletedProcess[str]
DockerTimeout = subprocess.TimeoutExpired

type DockerCommandOutcome = DockerResult | FileNotFoundError | DockerTimeout
type DockerCliOutcome = DockerResult | BaseException


def docker_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> DockerResult:
    """Return the completed ``docker`` invocation a scripted call should answer with."""
    return DockerResult((), returncode, stdout, stderr)


def docker_timed_out(argv: Sequence[str], seconds: float) -> DockerTimeout:
    """Return the timeout *argv* hits when it runs past *seconds*."""
    return DockerTimeout(list(argv), seconds)


def docker_missing() -> FileNotFoundError:
    """Return the error raised when the ``docker`` executable is not installed."""
    return FileNotFoundError("docker")


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
    ) -> DockerResult:
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


@dataclass(frozen=True, slots=True)
class DockerCliCall:
    """One ``DockerCli`` invocation; ``timeout_seconds`` is ``None`` for a spawn."""

    argv: tuple[str, ...]
    timeout_seconds: float | None


class _PrefixRule:
    """Outcomes for argv starting with ``prefix``; the last one repeats."""

    def __init__(self, prefix: tuple[str, ...], outcomes: tuple[DockerCliOutcome, ...]) -> None:
        self.prefix = prefix
        self._outcomes = outcomes
        self._next = 0

    def take(self) -> DockerCliOutcome:
        outcome = self._outcomes[min(self._next, len(self._outcomes) - 1)]
        self._next += 1
        return outcome


class ScriptedDockerCli:
    """A ``DockerCli`` that records every call and answers from a script.

    Answers come from, in order: the longest matching ``on`` prefix rule, the
    next unused ``then`` outcome, then the ``always`` fallback. A call nothing
    answers is an ``AssertionError`` (after being recorded), so a test states
    every Docker interaction it relies on.
    """

    def __init__(self) -> None:
        """Start with an empty script and call history."""
        self.calls: list[DockerCliCall] = []
        self._rules: list[_PrefixRule] = []
        self._sequence: deque[DockerCliOutcome] = deque()
        self._fallback: DockerCliOutcome | None = None

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        """Return the argv of every call so far, in order."""
        return [call.argv for call in self.calls]

    def on(self, prefix: Sequence[str], *outcomes: DockerCliOutcome) -> None:
        """Answer argv starting with *prefix* with *outcomes* in turn, the last repeating."""
        if not outcomes:
            message = "a scripted Docker prefix requires at least one outcome"
            raise ValueError(message)
        self._rules.append(_PrefixRule(tuple(prefix), outcomes))

    def then(self, *outcomes: DockerCliOutcome) -> None:
        """Answer the next calls no prefix rule matches, one outcome per call."""
        self._sequence.extend(outcomes)

    def always(self, outcome: DockerCliOutcome) -> None:
        """Answer every call nothing else answers with *outcome*."""
        self._fallback = outcome

    def clear_script(self) -> None:
        """Drop every scripted outcome; the call history is kept."""
        self._rules.clear()
        self._sequence.clear()
        self._fallback = None

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> DockerResult:
        """Return or raise the scripted outcome for *argv*."""
        call = DockerCliCall(tuple(argv), timeout_seconds)
        self.calls.append(call)
        outcome = self._outcome_for(call.argv)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def spawn(self, argv: Sequence[str]) -> NoReturn:
        """Record the call, then fail: this fake has no process handles to give out."""
        self.calls.append(DockerCliCall(tuple(argv), None))
        message = f"ScriptedDockerCli cannot spawn {tuple(argv)!r}; use FakeDockerEngine"
        raise AssertionError(message)

    def _outcome_for(self, argv: tuple[str, ...]) -> DockerCliOutcome:
        matching = [rule for rule in self._rules if argv[: len(rule.prefix)] == rule.prefix]
        if matching:
            return max(matching, key=lambda rule: len(rule.prefix)).take()
        if self._sequence:
            return self._sequence.popleft()
        if self._fallback is not None:
            return self._fallback
        message = f"no scripted Docker CLI outcome for {argv!r}"
        raise AssertionError(message)
