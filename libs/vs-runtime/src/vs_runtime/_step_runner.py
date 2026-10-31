"""The interface the runner loop calls to execute a step and to clean up after one.

A step is one request core asks the shell to perform; its tag is its request id. The
loop owns timers, tasks, cancellation and recording. A runner owns the mechanism: how a
step runs, and how what an earlier attempt left behind is found by tag.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId
    from vs_core.api.recovery import CancelReason, StepResult


@dataclass(frozen=True)
class Adopted:
    """``clear`` took over what an earlier attempt left.

    With a result, the step had ended and the next ``run`` returns that result. With
    none, the work is still going and the next ``run`` attaches to it instead of
    starting new work.
    """

    result: StepResult | None


@dataclass(frozen=True)
class Cleared:
    """``clear`` found nothing carrying the tag, or removed all of it."""


class ClearUnavailableError(Exception):
    """``clear`` could not reach its backend, so it proved nothing about the tag.

    The loop retries with backoff and records no result.
    """

    def __init__(self, tag: RequestId, backend: str) -> None:
        """Name the tag and the backend that could not be reached."""
        super().__init__(f"cannot clear step {tag}: {backend} is unreachable")
        self.tag = tag
        self.backend = backend


class CancelSignal:
    """Set once by the loop when a step must end; the runner watches it.

    The first reason wins: a later ``set`` does not change what the loop records.
    """

    def __init__(self) -> None:
        """Start unset."""
        self._event = asyncio.Event()
        self._reason: CancelReason | None = None

    @property
    def reason(self) -> CancelReason | None:
        """Why the step must end, or ``None`` while it need not."""
        return self._reason

    def is_set(self) -> bool:
        """Whether the step must end."""
        return self._reason is not None

    def set(self, reason: CancelReason) -> None:
        """Ask the step to end for ``reason``; the first reason is kept."""
        if self._reason is None:
            self._reason = reason
            self._event.set()

    async def wait(self) -> CancelReason:
        """Wait until the step must end, and say why."""
        while (reason := self._reason) is None:
            await self._event.wait()
        return reason


class StepRunner(Protocol):
    """Executes steps by tag, and removes or adopts what an earlier attempt left."""

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run attempt ``attempt`` of ``step`` and return its result, or raise.

        ``deadline_at`` is the run-clock second at which core cancels the attempt; a runner
        passes it on as the limit of the work it starts. A raise is "no result": the loop
        treats it like a crash of the attempt. Once ``cancel`` is set the runner returns
        ``Cancelled`` within the loop's grace period; the loop cancels the task otherwise and
        calls ``clear``.
        """
        ...

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Remove or adopt everything an earlier attempt of ``step`` left behind.

        ``step`` is the request the attempt ran, as the log holds it, so the runner finds
        its leftovers from the step itself and keeps no index from tag to step. Idempotent.
        Returns ``Cleared`` for a step that never started. Returns only when nothing carrying
        the tag runs or will start. Raises ``ClearUnavailableError`` when the backend cannot
        be reached.
        """
        ...
