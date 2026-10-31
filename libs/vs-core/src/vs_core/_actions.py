"""What core asks the runner loop to do next: the closed set ``decide`` returns.

No variant is blocked or unknown, and none names a step kind: a step is a ``Request`` the loop
hands to a runner whole.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .types.common import RunStatus
from .types.steps import CancelReason, Interruption

if TYPE_CHECKING:
    from .types.common import RequestId
    from .types.kernel import CoreState
    from .types.records import Request
    from .types.settlement import RunResultProposal
    from .types.steps import StepResult


@dataclass(frozen=True)
class Run:
    """Start the next attempt of an open step."""

    step: Request
    tag: RequestId
    attempt: int
    """1, or n + 1 after n attempts ended without a result."""
    deadline_at: float
    """Run-clock seconds; the loop sets a timer for it."""


@dataclass(frozen=True)
class Cancel:
    """Stop a running step; its attempt ends with a result or an interruption."""

    tag: RequestId
    reason: CancelReason


@dataclass(frozen=True)
class Settle:
    """End a step with a result core decided without running it."""

    tag: RequestId
    result: StepResult
    """Attempts spent, or ``on_interrupt`` is ``CANCEL`` and an attempt was interrupted."""


@dataclass(frozen=True)
class Wait:
    """Nothing to start now."""

    until: float | None
    """The next deadline on the run clock, or None to wake on any input."""


@dataclass(frozen=True)
class End:
    """The run committed its finish and nothing is open: publish it and exit."""

    result: RunResultProposal


type Action = Run | Cancel | Settle | Wait | End


@dataclass(frozen=True)
class OpenStep:
    """A step with no result, as the loop starts an attempt of it."""

    step: Request
    """The request to run."""
    deadline_at: float
    """The run-clock limit of the step, once it has started."""
    started: int
    """The latest attempt that began; 0 before the first."""


def interruption_of(reason: CancelReason) -> Interruption | None:
    """How the attempt of a cancelled step ends, or None when the cancellation is its result.

    A pause cancels an attempt without ending its step: the step runs again when the run
    resumes, so the attempt is recorded as interrupted and the step stays open. Every other
    reason is the step's result.
    """
    match reason:
        case CancelReason.PAUSE_NOW | CancelReason.DRAIN_EXPIRED:
            return Interruption.PAUSE
        case (
            CancelReason.DEADLINE
            | CancelReason.RUN_ENDING
            | CancelReason.WITHDRAWN
            | CancelReason.INTERRUPTED
        ):
            return None


class RunRest(StrEnum):
    """Why the runner loop has nothing more to do for this process."""

    ENDED = "ended"
    """The run's end is in the log."""

    PARKED = "parked"
    """The run is paused: it starts nothing, and exits once nothing is in flight."""


def at_rest(state: CoreState) -> RunRest | None:
    """Whether the run ended or parked, else None while it goes on.

    A run has ended when its end is recorded, not when the kernel reaches its terminal status: a
    stop with nothing open reaches it at once, and the result is still to be published.
    """
    if state.run.result is not None and state.steps.ended == state.run.result:
        return RunRest.ENDED
    return RunRest.PARKED if state.run.status is RunStatus.PARKED else None
