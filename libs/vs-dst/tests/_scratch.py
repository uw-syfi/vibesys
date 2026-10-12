"""The reference a cheap crash is measured against: run again from the start with a crash planted.

The host dies at the ``commit``-th commit, just before or just after it reaches the store; a new
host (a new `CoreRuntime` over a new strategy instance) starts over the store the dead one left,
and the executors keep their world. This is the slow way to a crash, and the simple one: nothing
is copied and nothing is shared except what the outside world is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_dst.api import CrashKind, CrashPoint, Effects, RecoverableExecutors
from vs_project.api import FakeStateStore
from vs_runtime.api.testing import LEASE, FakePublicationDelivery, HostOptions, scripted_host

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState, Decision, StrategyState
    from vs_dst.api import World
    from vs_project.api import CommitOutcome, StoredEnvelope, StoreFence


class HostDiedError(BaseException):
    """The process died. A `BaseException`, so nothing in the loop can absorb it."""


class CrashingStore(FakeStateStore):
    """A store that kills its host around one commit."""

    def __init__(self, point: CrashPoint) -> None:
        super().__init__()
        self.commits = 0
        self._point = point

    def commit(
        self,
        expected_revision: int | None,
        envelope: StoredEnvelope,
        fence: StoreFence,
        now: float,
    ) -> CommitOutcome:
        index = self.commits
        self.commits += 1
        chosen = self._point.commit == index
        if chosen and self._point.kind is CrashKind.BEFORE_COMMIT:
            raise HostDiedError
        outcome = super().commit(expected_revision, envelope, fence, now)
        if chosen:
            raise HostDiedError
        return outcome


@dataclass
class Survived:
    """A run that was killed once and finished."""

    core: CoreState
    state: StrategyState
    decisions: list[Decision]
    effects: Effects


def run_with_crash(new_world: Callable[[], World], point: CrashPoint) -> Survived:
    """Run a fresh world to the end, killing the host once at ``point``."""
    world = new_world()
    store = CrashingStore(point)
    effects = Effects()
    delivery = FakePublicationDelivery(store)
    options = HostOptions(
        translator=lambda script, core, registry, schemas, journal: RecoverableExecutors(
            script, core, registry, schemas, journal, effects
        )
    )
    decisions: list[Decision] = []
    clock_at = 1.0
    died = False
    while True:
        setup = world.setup
        host = scripted_host(
            setup.new_strategy(),
            world,
            setup.harness,
            setup.schemas,
            options,
            store=store,
            delivery=delivery,
            clock_at=clock_at,
        )
        try:
            run = host.run()
        except HostDiedError:
            died = True
            decisions.extend(host.decisions)
            # The next host starts after the dead one's lease has expired.
            clock_at += LEASE + 1
            continue
        assert died, f"{point} did not crash the host"
        decisions.extend(run.decisions)
        return Survived(run.core, run.state, decisions, effects)
