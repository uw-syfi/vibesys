"""Faults at the host boundaries: executor requests and durable writes.

:class:`FaultGate` is the one counter a simulated host process shares across restarts. A
wrapper (around the executors, around the durable store) calls :meth:`FaultGate.around`
with its boundary and a target key; the gate counts the call, records it, and, when the
plan has a rule for that ordinal, raises :class:`HostCrashError` after the call took
effect. With no rule it is a pass-through. ``calls`` is the run's own list of boundary
crossings, so a test can enumerate every crash point from a crash-free run.

The gate knows no request kinds or record layouts: a target is any string the wrapper
chooses. Fault kinds beyond the crash are new :class:`~vs_faults.plan.HostFault` members
handled here, so wrappers do not change.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from vs_faults.plan import Boundary, FaultPlan, HostFault

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_HOST_BOUNDARIES = (Boundary.EXECUTOR_REQUEST, Boundary.DURABLE_WRITE)


class HostCrashError(RuntimeError):
    """The host process died right after a call took effect (an injected fault)."""

    def __init__(self, boundary: Boundary, target: str, ordinal: int) -> None:
        """Name the crash point, which is also the rule that reproduces it."""
        self.boundary = boundary
        self.target = target
        self.ordinal = ordinal
        super().__init__(f"host crashed after {boundary.value} {target} #{ordinal}")


@dataclass(frozen=True)
class Crossing:
    """One boundary crossing: the ``ordinal``-th call at ``boundary`` on ``target``."""

    boundary: Boundary
    target: str
    ordinal: int


@dataclass
class FaultGate:
    """Counts host-boundary calls across restarts and crashes after the scheduled ones.

    ``heal`` drops the plan so the rest of the run is fault-free (counting continues): the
    heal-then-liveness phase of a simulated run.
    """

    plan: FaultPlan
    calls: list[Crossing] = field(default_factory=list)
    _counts: Counter[tuple[Boundary, str]] = field(default_factory=Counter)

    def heal(self) -> None:
        """Stop injecting faults."""
        self.plan = FaultPlan(seed=self.plan.seed)

    def _enter(self, boundary: Boundary, target: str) -> Crossing:
        if boundary not in _HOST_BOUNDARIES:
            message = f"not a host boundary: {boundary.value}"
            raise ValueError(message)
        self._counts[boundary, target] += 1
        crossing = Crossing(boundary, target, self._counts[boundary, target])
        self.calls.append(crossing)
        return crossing

    def _after(self, crossing: Crossing) -> None:
        rule = self.plan.match(crossing.boundary, crossing.target, crossing.ordinal)
        if rule is not None and cast("HostFault", rule.fault) is HostFault.CRASH_AFTER:
            raise HostCrashError(crossing.boundary, crossing.target, crossing.ordinal)

    def around[T](self, boundary: Boundary, target: str, call: Callable[[], T]) -> T:
        """Run ``call``, then crash the host if the plan says so."""
        crossing = self._enter(boundary, target)
        result = call()
        self._after(crossing)
        return result

    async def around_async[T](
        self, boundary: Boundary, target: str, call: Callable[[], Awaitable[T]]
    ) -> T:
        """Run the awaitable ``call``, then crash the host if the plan says so."""
        crossing = self._enter(boundary, target)
        result = await call()
        self._after(crossing)
        return result
