"""A ``StepRunner`` that stops at a chosen point of the steps of chosen kinds, for a test to act at.

A kill or a pause lands in a window of a step: before its work starts, or after the work is done
and before its result is returned, which is the worst one (the effect happened and nothing
records it). The decorator names those windows at the one seam every step crosses, so no
executor carries a hook. ``hold`` is called at the chosen point and the step goes on when it
returns; what it does there (report, wait for a release) belongs to the test.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_core.api import Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_runtime._step_runner import Adopted, CancelSignal, Cleared, StepRunner


class HoldAt(StrEnum):
    """The window of a step the decorator holds."""

    BEFORE = "before"
    """Before the inner runner is called: the attempt is recorded and nothing has been done."""
    AFTER_EFFECT = "after-effect"
    """After the inner runner returned its result and before the loop gets it."""


class HoldingStepRunner:
    """``inner``, with every step of a kind in ``held`` stopped at ``where`` until ``hold`` returns."""

    def __init__(
        self,
        inner: StepRunner,
        held: frozenset[type[Request]],
        where: HoldAt,
        hold: Callable[[Request, RequestId, int], Awaitable[None]],
    ) -> None:
        """Hold the steps whose request is exactly one of the kinds in ``held``.

        ``hold`` receives the step, its tag and the attempt, and may be called once per attempt.
        """
        self._inner = inner
        self._held = held
        self._where = where
        self._hold = hold

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run the step on ``inner``, stopping at the chosen window if its kind is held."""
        holds = type(step) in self._held
        if holds and self._where is HoldAt.BEFORE:
            await self._hold(step, tag, attempt)
        result = await self._inner.run(step, tag, attempt, deadline_at, cancel)
        if holds and self._where is HoldAt.AFTER_EFFECT:
            await self._hold(step, tag, attempt)
        return result

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Clear on ``inner``: a hold is not work, so nothing of it is left."""
        return await self._inner.clear(step, tag)
