"""One test's simulation: its clock, seed, random streams, gates and registered domain fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vs_sim.child import run_in_child
from vs_sim.gate import Gate
from vs_sim.os_platform import Platform
from vs_sim.randomness import SeededRandom, derive_seed
from vs_sim.sim_network import DEFAULT_BUFFER_BYTES, SimNetwork
from vs_sim.sim_threads import SimBlockingRunner, SimThreads
from vs_sim.virtual import VirtualClock, WallClockView, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from vs_sim.processes import Processes
    from vs_sim.signals import ProcessSignaller
    from vs_sim.sim_network import LinkFault
    from vs_sim.trace import EventTrace

type WorldFactory = Callable[[Sim], object]
"""Builds one domain fake for a test from that test's :class:`Sim`."""


class UnknownWorldError(KeyError):
    """``Sim.world`` was asked for a name nothing registered."""


class WorldRegistry:
    """The domain fakes a project makes available to ``Sim.world``, by name.

    The kit knows no domain: a project registers a factory per fake in its own conftest, and
    every test then builds the fake on its own clock and seed with ``sim.world(name)``.
    """

    def __init__(self) -> None:
        """Start with no factories."""
        self._factories: dict[str, WorldFactory] = {}

    def register(self, name: str, factory: WorldFactory) -> None:
        """Make ``factory`` the builder for ``name``; a name has one factory."""
        if name in self._factories:
            message = f"a world named {name!r} is already registered"
            raise ValueError(message)
        self._factories[name] = factory

    def factory(self, name: str) -> WorldFactory:
        """The factory registered for ``name``."""
        try:
            return self._factories[name]
        except KeyError:
            known = ", ".join(sorted(self._factories)) or "none"
            message = f"no world named {name!r} is registered (registered: {known})"
            raise UnknownWorldError(message) from None

    @property
    def names(self) -> tuple[str, ...]:
        """The registered names, sorted."""
        return tuple(sorted(self._factories))


WORLDS = WorldRegistry()
"""The process-wide registry that the pytest plugin's ``sim`` fixture reads."""


@dataclass
class Sim:
    """What a simulated test gets: everything that would otherwise depend on timing or luck."""

    seed: int
    clock: VirtualClock = field(default_factory=VirtualClock)
    worlds: WorldRegistry = field(default_factory=lambda: WORLDS)
    trace: EventTrace | None = None
    schedule_seed: int | None = None
    """When set, ties between ready work are broken in an order drawn from this seed."""
    _built: dict[str, object] = field(default_factory=dict, init=False, repr=False)

    def run[T](self, main: Coroutine[object, object, T], *, threads: SimThreads | None = None) -> T:
        """Run ``main`` to completion on this simulation's virtual clock.

        Simulated ``threads`` (from :meth:`threads`) run whenever the loop is idle.
        """
        return run_virtual(
            self.clock, main, trace=self.trace, schedule_seed=self.schedule_seed, driver=threads
        )

    def threads(self) -> SimThreads:
        """Simulated threads on this simulation's clock, schedule seed and trace."""
        return SimThreads(self.clock, schedule_seed=self.schedule_seed, trace=self.trace)

    @property
    def wall_clock(self) -> WallClockView:
        """This simulation's timeline read as wall-clock time: seconds since the epoch.

        It ticks with :attr:`clock` (near zero) and :meth:`SimThreads.now` (near
        ``SIM_MONOTONIC_ORIGIN``) but reads near ``SIM_WALL_EPOCH``, so code that mixes a
        timestamp with a monotonic reading is off by decades here instead of passing by
        coincidence.
        """
        return WallClockView(self.clock)

    def platform(
        self,
        threads: SimThreads,
        *,
        processes: Processes,
        signaller: ProcessSignaller,
        network_faults: Sequence[LinkFault] = (),
        network_buffer_bytes: int = DEFAULT_BUFFER_BYTES,
    ) -> Platform:
        """A :class:`Platform` on this simulation's timeline, over ``threads`` (from :meth:`threads`).

        Its clock is :attr:`wall_clock`; ``threads.now()`` is the monotonic reading of the same
        timeline. Blocking calls and sockets run on ``threads``; processes and signals are the
        Fakes given. The network fires ``network_faults`` and holds ``network_buffer_bytes``
        unread per direction of a connection (see :class:`SimNetwork`).
        """
        return Platform(
            clock=self.wall_clock,
            threads=threads,
            blocking=SimBlockingRunner(threads),
            processes=processes,
            network=SimNetwork(threads, buffer_bytes=network_buffer_bytes, faults=network_faults),
            signaller=signaller,
        )

    def random(self, label: str = "") -> SeededRandom:
        """The random stream ``label`` draws from; independent of every other label."""
        return SeededRandom(derive_seed(self.seed, label))

    def gate(self) -> Gate:
        """A new closed gate: a wait tied to the lifetime of what should open it."""
        return Gate()

    def run_in_child[T](self, function: Callable[[], T]) -> T:
        """Run ``function`` in a child process, for code that changes process-wide state."""
        return run_in_child(function)

    def world(self, name: str) -> Any:  # noqa: ANN401  # LW-163807 [ANN401]; each registered factory returns its own fake type and the registry is untyped by design.
        """The domain fake registered as ``name``, built once for this test from this Sim."""
        if name not in self._built:
            self._built[name] = self.worlds.factory(name)(self)
        return self._built[name]
