"""What core asks the runner loop to do next: the closed set ``decide`` returns.

No variant is blocked or unknown, and none names a step kind: a step is a ``Request`` the loop
hands to a runner whole.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types.common import RequestId
    from .types.records import Request
    from .types.settlement import RunResultProposal
    from .types.steps import CancelReason, StepResult


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
