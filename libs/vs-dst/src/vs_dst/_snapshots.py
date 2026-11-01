"""Copies of the outside world at the boundaries of a run.

A host that dies loses its memory, so what survives a crash is the outside world: the run.log,
the effects ledger, and the world's own position (the replies it has handed out). `Snapshots`
copies exactly those just before and just after every commit of the run it watches, and as each
effect begins, in one `copy.deepcopy` so objects that share a reference keep sharing it. A crash case then starts a new host on a copy instead of re-running
the prefix.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_dst._interruptions import Action, Interruption, Moment
from vs_runlog.api import MemoryFoldCache
from vs_runtime.api.testing import FakeRunRecords

if TYPE_CHECKING:
    from vs_core.api import CoreEvent, Decision, StopMode
    from vs_dst._effects import Effects
    from vs_dst._world import World
    from vs_proto.api import Record
    from vs_runtime.api.step_runner import FoldStart
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
        """Hand the run an input core delivers itself (the deadline's stop): no channel carries it."""
        ...

    def pause(self, control_id: str) -> None:
        """The operator's Pause, sent to the control channel as the socket and the session send it."""
        ...

    def stop(self, mode: StopMode, control_id: str) -> None:
        """The operator's Stop, sent to the control channel."""
        ...

    def environment_stop(self) -> None:
        """The run environment's stop: the host cancels the run for good."""
        ...

    def quota_pause(self, seconds: float) -> None:
        """The capacity gate pauses the run on the provider's quota, which lifts after ``seconds``.

        The turns in flight wait in their calls while the pause holds. A host that ends first
        never lifts it: the timer died with the process.
        """
        ...


class Store(Protocol):
    """What survives a host and counts its commits: the durable side of a run."""

    commits: int
    """How many commits the host was asked for."""
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

    store: Store
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
    record, a kill just after leaves it. The run's ``state.pb`` is kept beside it, as it is
    beside the file: it survives a host and is copied with the log.
    """

    def __init__(self) -> None:
        """Start empty, watched by nobody."""
        super().__init__()
        self.commits = 0
        self.last_clock = 0.0
        """The latest run-clock time of a record in the log."""
        self.watcher: Watcher | None = None
        self.cache = MemoryFoldCache()
        """The fold cache of the run (``state.pb``), which a host's start reads and its log writes."""
        self.starts: list[FoldStart] = []
        """How each host that opened the log got its fold: from the cache, or the whole log and why."""

    def __deepcopy__(self, memo: dict[int, object]) -> RecordingRecords:
        """A copy is the log of the outside world, not of the watching: it has no watcher."""
        copied = RecordingRecords()
        memo[id(self)] = copied
        copied.records = copy.deepcopy(self.records, memo)
        copied.commits = self.commits
        copied.last_clock = self.last_clock
        copied.cache = copy.deepcopy(self.cache, memo)
        copied.starts = list(self.starts)
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
