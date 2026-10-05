"""Seeded fault injection at the executor boundary of the skeleton's shell.

``FaultingExecutors`` wraps the real executors and, at the Nth effect of a run (a count
that survives restarts), injects one fault: deliver the request twice (duplicate
delivery), or let the effect happen and then crash the process before its observation is
committed. No sleeps and no wall clock: a schedule is a plain mapping from effect index to
fault, so Hypothesis can generate and shrink it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_runtime.api.core import RequestExecutors

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import Request
    from vs_runtime.api.core import ExecutionContext, ExecutionOutcome


class Fault(StrEnum):
    """What goes wrong at one effect."""

    DUPLICATE = "duplicate-delivery"
    CRASH_AFTER_EFFECT = "crash-after-effect-before-receipt"


class InjectedCrashError(RuntimeError):
    """The host process died after an effect and before its observation was committed."""


@dataclass
class FaultSchedule:
    """Which fault hits which effect, counted across every process of the run."""

    at: Mapping[int, Fault] = field(default_factory=dict)
    effects: int = 0
    crashes: int = 0

    def next(self) -> Fault | None:
        """Count one effect and return the fault scheduled for it, if any."""
        index = self.effects
        self.effects += 1
        return self.at.get(index)


@dataclass(frozen=True)
class FaultingExecutors(RequestExecutors):
    """The real executors with a fault schedule at their single dispatch point."""

    schedule: FaultSchedule = field(default_factory=FaultSchedule)

    @classmethod
    def around(cls, real: RequestExecutors, schedule: FaultSchedule) -> FaultingExecutors:
        """Wrap ``real``; every role is the real executor."""
        return cls(
            workspaces=real.workspaces,
            sessions=real.sessions,
            evaluation=real.evaluation,
            operations=real.operations,
            semantic_events=real.semantic_events,
            schedule=schedule,
        )

    async def dispatch(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        """Run the effect, injecting the scheduled fault around it."""
        fault = self.schedule.next()
        outcome = await super().dispatch(request, context)
        if fault == Fault.DUPLICATE:
            return await super().dispatch(request, context)
        if fault == Fault.CRASH_AFTER_EFFECT:
            self.schedule.crashes += 1
            raise InjectedCrashError(request.kind)
        return outcome
