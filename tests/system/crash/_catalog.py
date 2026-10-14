"""The crash points of the skeleton run, grouped by boundary kind and request lifecycle class.

A host can die at every crossing of the shell's two boundaries (an executor request, a durable
write). Recovery does not depend on which of the run's repeated steps was in flight; it depends on
what the dying host had made durable (the boundary kind) and on how core recovers the request that
was in flight (its lifecycle class: a query is asked again, an idempotent write is issued again
under its key, an owned job and a session turn are inspected first). So a pull request crashes the
host once per (boundary kind, lifecycle class) and the nightly job crashes it at every point of
each group. `vs_sim.api.testing.swept` makes that choice, from the session's `--depth`.

A crash inside the recovery of an earlier crash is one more boundary kind (`RECOVERY`), not a
second sweep: its points are the crossings of the recovery window that follows the reference crash
of each lifecycle class.
"""

from __future__ import annotations

import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import (
    after_crash,
    all_crossings,
    crash_plan,
    name,
    rule,
    run,
    straight_run,
)
from tests.support.skeleton_faults import COMMIT, RECEIPT_BEGUN, RECEIPT_SEALED
from tests.support.skeleton_sim import Simulation, simulate

from vs_faults.api import Boundary, Crossing, FaultGate, FaultPlan, HostCrashError
from vs_sim.api.testing import Depth, VirtualClock, depth, examples, run_virtual, swept

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import LifecycleClass


class PointKind(StrEnum):
    """What the host had made durable, or done, when it died."""

    OBSERVATION_LOST = "observation_lost"
    """A request's effect ran and the host died before core recorded its observation."""
    AUTHORIZED = "authorized"
    """Core authorized a request (a commit) and the host died before it began."""
    BEGUN = "begun"
    """The request's begun marker landed and the host died before its effect ran."""
    SEALED = "sealed"
    """The effect ran and its sealed result landed; the host died before core heard of it."""
    SEAL_LOST = "seal_lost"
    """The effect ran and the host died before its sealed result landed."""
    TRANSITION = "transition"
    """Any other commit: core moved from one state to the next between two requests."""
    RECOVERY = "recovery"
    """A second death while the restarted host is still recovering from the first."""


@dataclass(frozen=True)
class Point:
    """One crash of the run: a kind of boundary, the request class in flight and where."""

    kind: PointKind
    lifecycle: LifecycleClass | None
    """The class of the request in flight; none before the run's first request."""
    crossing: Crossing
    """The crossing the host dies at (for ``RECOVERY``, the second death)."""
    first: Crossing | None = None
    """For ``RECOVERY``, the crossing of the first death."""

    @property
    def id(self) -> str:
        """A name that says what the point is, for the test report."""
        where = (
            name(self.crossing)
            if self.first is None
            else f"{name(self.first)}+{name(self.crossing)}"
        )
        return f"{self.kind.value}:{self.lifecycle.value if self.lifecycle else 'run'}:{where}"


@cache
def _lifecycles() -> dict[tuple[str, int], LifecycleClass]:
    """The lifecycle class of the n-th request of each kind, as core recorded the straight run."""
    by_kind: dict[str, list[LifecycleClass]] = defaultdict(list)
    for record in straight_run().core.records.records:
        by_kind[record.request.kind].append(record.lifecycle)
    requests = Counter(c.target for c in all_crossings() if c.boundary == Boundary.EXECUTOR_REQUEST)
    assert {kind: len(found) for kind, found in by_kind.items()} == dict(requests), (
        "core's records and the executor crossings of the straight run disagree"
    )
    return {
        (kind, n): found[n - 1] for kind, found in by_kind.items() for n in range(1, len(found) + 1)
    }


def lifecycle_of(request: Crossing) -> LifecycleClass:
    """The class of the request an executor-request crossing is."""
    return _lifecycles()[request.target, request.ordinal]


@cache
def points() -> tuple[Point, ...]:
    """Every crash point of the straight run that needs no earlier crash, classified."""
    crossings = all_crossings()
    found: list[Point] = []
    current: LifecycleClass | None = None
    for index, crossing in enumerate(crossings):
        following = crossings[index + 1 : index + 2]
        if crossing.boundary == Boundary.EXECUTOR_REQUEST:
            current = lifecycle_of(crossing)
            found.append(Point(PointKind.OBSERVATION_LOST, current, crossing))
        elif crossing.target == RECEIPT_BEGUN:
            found.append(Point(PointKind.BEGUN, current, crossing))
        elif crossing.target == RECEIPT_SEALED:
            found.append(Point(PointKind.SEALED, current, crossing))
            found.append(Point(PointKind.SEAL_LOST, current, crossing))
        elif (
            crossing.target == COMMIT
            and following
            and following[0].boundary == Boundary.EXECUTOR_REQUEST
        ):
            found.append(Point(PointKind.AUTHORIZED, lifecycle_of(following[0]), crossing))
        else:
            assert crossing.target == COMMIT, name(crossing)
            found.append(Point(PointKind.TRANSITION, current, crossing))
    return tuple(found)


def groups(kind: PointKind) -> dict[LifecycleClass | None, list[Point]]:
    """The points of ``kind``, by the lifecycle class of the request in flight."""
    grouped: dict[LifecycleClass | None, list[Point]] = defaultdict(list)
    for point in points():
        if point.kind == kind:
            grouped[point.lifecycle].append(point)
    return dict(grouped)


def sampled(kind: PointKind) -> list[Point]:
    """The points of ``kind`` this session tries: one per lifecycle class, or all of them.

    A transition is a commit between two requests: which request is in flight does not change
    what the crash leaves behind, so a pull request tries one transition, not one per class.
    """
    grouped = list(groups(kind).values())
    if kind == PointKind.TRANSITION and depth() is not Depth.NIGHTLY:
        grouped = [max(grouped, key=len)]
    return [group[i] for group in grouped for i in swept(len(group), 1)]


_INSPECTION = "inspect_request"


@cache
def recovery_window(first: Crossing) -> tuple[Crossing, ...]:
    """The crossings of the restarted host that are recovery: until its first ordinary request.

    From the restart until the first request that is not an inspection, so each recovery write
    and each inspection is a crash point.
    """
    calls = run(crash_plan(first)).gate.calls
    window: list[Crossing] = []
    for crossing in after_crash(calls, first):
        if crossing.boundary == Boundary.EXECUTOR_REQUEST and crossing.target != _INSPECTION:
            break
        window.append(crossing)
    # The write that authorizes the first ordinary request is a dispatch, not recovery.
    return tuple(window[:-1]) if window and window[-1].boundary == Boundary.DURABLE_WRITE else ()


def sampled_recoveries(lifecycle: LifecycleClass, part: int = 0, parts: int = 1) -> list[Point]:
    """Second deaths in the recovery of the reference crashes of one lifecycle class.

    The reference crashes are those of ``sampled(OBSERVATION_LOST)`` of the class. A pull request
    tries the first crossing of each window; the nightly job tries every crossing, in ``parts``
    interleaved parts so that no test outlasts the hang bound. Computing the windows runs the
    simulation, so it happens in the test and not at collection.
    """
    pairs = [
        Point(PointKind.RECOVERY, lifecycle, second, reference.crossing)
        for reference in sampled(PointKind.OBSERVATION_LOST)
        if reference.lifecycle == lifecycle
        for second in recovery_window(reference.crossing)
    ]
    return [pairs[i] for i in swept(len(pairs), 1)][part::parts]


def recovery_parts() -> int:
    """How many parts the exhaustive recovery sweep of a class is split into."""
    return examples(pr=1, nightly=4)


def params(chosen: list[Point]) -> list[object]:
    """The points as pytest parameters named by ``Point.id``."""
    return [pytest.param(point, id=point.id) for point in chosen]


class LostSealGate(FaultGate):
    """A gate whose planned crash at a sealed write comes before the write lands.

    ``run_once`` writes ``begun``, runs the effect, then seals the result. The other crashes
    come after a write lands, so they never leave an effect that ran with no sealed result. Here
    the sealed write never lands, which is the state a host killed in the middle of a job poll or
    an agent turn leaves behind.
    """

    def around[T](self, boundary: Boundary, target: str, call: Callable[[], T]) -> T:
        """Crash instead of writing when the plan names this sealed write."""
        ordinal = 1 + sum(1 for c in self.calls if (c.boundary, c.target) == (boundary, target))
        if target != RECEIPT_SEALED or self.plan.match(boundary, target, ordinal) is None:
            return super().around(boundary, target, call)

        def lost() -> T:
            raise HostCrashError(boundary, target, ordinal)

        return super().around(boundary, target, lost)


def run_with_lost_seal(seal: Crossing) -> Simulation:
    """The run in which the host dies just before ``seal`` would land."""
    plan = FaultPlan(seed=seal.ordinal, rules=(rule(seal),))
    with tempfile.TemporaryDirectory() as tmp:
        return run_virtual(VirtualClock(), simulate(Path(tmp), plan, gate=LostSealGate(plan)))
