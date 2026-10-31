"""Copies of the outside world at the boundaries of a run.

A host that dies loses its memory, so what survives a crash is the outside world: the store,
the effects ledger, the publication delivery, and the world's own position (the replies it has
handed out). `Snapshots` copies exactly those just before and just after every commit of the
run it watches, and as each effect begins, in one `copy.deepcopy` so objects that share a
reference keep sharing it. A crash case then starts a new host on a copy instead of re-running
the prefix.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from vs_dst._interruptions import Action, Interruption, Moment
from vs_project.api import FakeStateStore
from vs_runtime.api.testing import FakeRunRecords

if TYPE_CHECKING:
    from vs_core.api import CoreEvent, Decision
    from vs_dst._effects import Effects
    from vs_dst._world import World
    from vs_project.api import CommitOutcome, StoredEnvelope, StoreFence
    from vs_proto.api import Record
    from vs_runtime.api.testing import FakePublicationDelivery
    from vs_sim.api.testing import VirtualClock


class RunningHost(Protocol):
    """What an interruption needs of the host it lands on: its clock, an input, its decisions."""

    @property
    def clock(self) -> VirtualClock:
        """The host's virtual clock."""
        ...

    @property
    def decisions(self) -> list[Decision]:
        """Every decision the host's search proposed so far."""
        ...

    def submit(self, event: CoreEvent) -> None:
        """Hand the run an input from outside its loop, as an operator does."""
        ...


class Store(Protocol):
    """What survives a host and counts its commits: the durable side of a run."""

    commits: int
    """How many commits the host was asked for."""
    lease_ends_at: float
    """The latest moment the committing hosts held a lease until; 0 when the run has none."""
    last_clock: float
    """The latest run-clock time a committed record carries; 0 before the first."""
    watcher: Watcher | None

    def observe(self) -> object | None:
        """What the store holds of the run, None before its first commit."""
        ...


class Watcher(Protocol):
    """Hears each moment of a run an interruption can land on, as it is reached."""

    def watch(self, store: Store, host: RunningHost) -> None:
        """Start hearing ``store``'s commits, for the run ``host`` drives."""
        ...

    def reached(self, moment: Moment, index: int) -> None:
        """The run is at the ``index``-th commit's ``moment``, or the ``index``-th effect in flight."""
        ...


class NondeterministicWorldError(RuntimeError):
    """Running the same world twice did not make the same number of commits."""

    def __init__(self, world: object, first: int, second: int) -> None:
        name = f"{type(world).__module__}.{type(world).__qualname__}"
        super().__init__(
            f"{name} is not deterministic: its run made {first} commits, and the same run "
            f"again made {second}, so a crash point of one is not a crash point of the other"
        )


class WorldNotCopyableError(TypeError):
    """A crash sweep copies the world at every commit, and this world cannot be copied."""

    def __init__(self, world: object) -> None:
        name = f"{type(world).__module__}.{type(world).__qualname__}"
        super().__init__(
            f"{name} cannot be deep-copied, so a crash case cannot start from a copy of it: "
            "keep the world's position in plain data (lists, deques, dicts, models), not in "
            "locks, generators, open files or threads"
        )


@dataclass
class OutsideWorld:
    """Everything outside a host: what survives it, and the one run's host starts over."""

    store: RecordingStore | RecordingRecords
    delivery: FakePublicationDelivery
    effects: Effects
    world: World

    def copy(self) -> OutsideWorld:
        """An independent copy of the whole outside world, shared references kept shared.

        The world's `setup` is immutable and is shared, not copied.
        """
        memo: dict[int, object] = {id(self.world.setup): self.world.setup}
        try:
            return copy.deepcopy(self, memo)
        except (TypeError, copy.Error, AttributeError) as error:
            raise WorldNotCopyableError(self.world) from error


@dataclass(frozen=True)
class Snapshot:
    """The outside world at one boundary, and how much the dead host had decided by then."""

    outside: OutsideWorld
    decisions: int


class RecordingRecords(FakeRunRecords):
    """A run.log kept in memory that tells its watcher before and after each append.

    Each append of a record is one commit: a kill just before it leaves the log without the
    record, a kill just after leaves it.
    """

    def __init__(self) -> None:
        """Start empty, watched by nobody."""
        super().__init__()
        self.commits = 0
        self.lease_ends_at = 0.0
        self.last_clock = 0.0
        self.watcher: Watcher | None = None

    def __deepcopy__(self, memo: dict[int, object]) -> RecordingRecords:
        """A copy is the log of the outside world, not of the watching: it has no watcher."""
        copied = RecordingRecords()
        memo[id(self)] = copied
        copied.records = copy.deepcopy(self.records, memo)
        copied.commits = self.commits
        copied.last_clock = self.last_clock
        return copied

    def observe(self) -> object | None:
        """The records, None before the first."""
        return tuple(self.records) or None

    def append(self, record: Record) -> None:
        """Take one record, with the watcher told just before it is durable and just after."""
        index = self.commits
        self.commits += 1
        if self.watcher is not None:
            self.watcher.reached(Moment.BEFORE_COMMIT, index)
        super().append(record)
        self.last_clock = max(self.last_clock, record.clock or 0.0)
        if self.watcher is not None:
            self.watcher.reached(Moment.AFTER_COMMIT, index)


class RecordingStore(FakeStateStore):
    """A store that tells its watcher before and after each commit, and counts them."""

    def __init__(self) -> None:
        """Start empty, watched by nobody."""
        super().__init__()
        self.commits = 0
        self.lease_ends_at = 0.0
        """The latest moment the committing hosts held the lease until, as the commits showed it."""
        self.watcher: Watcher | None = None

    def __deepcopy__(self, memo: dict[int, object]) -> RecordingStore:
        """A copy is a store of the outside world, not of the watching: it has no watcher."""
        memo[id(self.watcher)] = None
        return cast("RecordingStore", super().__deepcopy__(memo))

    def commit(
        self,
        expected_revision: int | None,
        envelope: StoredEnvelope,
        fence: StoreFence,
        now: float,
    ) -> CommitOutcome:
        """Commit, with the watcher told just before the store changes and just after."""
        index = self.commits
        self.commits += 1
        self.lease_ends_at = max(self.lease_ends_at, fence.expires_at, now)
        if self.watcher is not None:
            self.watcher.reached(Moment.BEFORE_COMMIT, index)
        outcome = super().commit(expected_revision, envelope, fence, now)
        if self.watcher is not None:
            self.watcher.reached(Moment.AFTER_COMMIT, index)
        return outcome


class Snapshots:
    """A copy of the outside world at each wanted moment of the run being watched.

    A wanted moment is a kill: what a copy holds is what a host that died there leaves behind.
    """

    def __init__(self, outside: OutsideWorld, wanted: frozenset[Interruption]) -> None:
        """Copy ``outside`` at the ``wanted`` points once `watch` has said whose decisions to count."""
        self._outside = outside
        self._wanted = wanted
        self._decisions: list[Decision] = []
        self.by_point: dict[Interruption, Snapshot] = {}

    def watch(self, store: Store, host: RunningHost) -> None:
        """Start watching the commits of ``store``, counting what ``host`` has decided."""
        self._decisions = host.decisions
        store.watcher = self

    def reached(self, moment: Moment, index: int) -> None:
        """Copy the outside world as it stands at this moment, if the sweep wants it."""
        point = Interruption(Action.KILL, moment, index)
        if point in self._wanted:
            self.by_point[point] = Snapshot(self._outside.copy(), len(self._decisions))
