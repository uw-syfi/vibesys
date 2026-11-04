"""Pure admission and retirement decisions for a session process."""

from dataclasses import dataclass, replace
from enum import StrEnum


class SessionAction(StrEnum):
    ANSWER = "answer"
    WAIT = "wait"
    EXIT = "exit"


@dataclass(frozen=True)
class SessionLifecycle:
    idle_since: float
    epoch: int
    idle_seconds: float = 300.0

    def next(self, now: float, epoch: int, *, parked: bool, pending: bool) -> SessionAction:
        """An epoch replacement or park retires the process before another turn."""
        if parked or epoch != self.epoch:
            return SessionAction.EXIT
        if pending:
            return SessionAction.ANSWER
        return (
            SessionAction.EXIT if now - self.idle_since >= self.idle_seconds else SessionAction.WAIT
        )

    def answered(self, now: float) -> "SessionLifecycle":
        """An idle deadline begins when the last answer finishes."""
        return replace(self, idle_since=now)
