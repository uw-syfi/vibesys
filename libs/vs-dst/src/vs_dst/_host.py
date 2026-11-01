"""One host of a run: a new process over the outside world, run until it ends."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from vs_core.api import RunStatus
from vs_core.api.recovery import RunRest, at_rest
from vs_runtime.api.testing import Run

if TYPE_CHECKING:
    from vs_core.api import CoreState
    from vs_dst._snapshots import OutsideWorld, Store, Watcher
    from vs_runtime.api.testing import HostOptions

FIRST_HOST_STARTS_AT = 1.0
"""The virtual time the first host starts at."""


def has_ended(core: CoreState) -> bool:
    """Whether the run is over: its kernel is terminal, or its end is recorded in the log."""
    return core.run.status == RunStatus.TERMINAL or at_rest(core) is RunRest.ENDED


class Hosting(Protocol):
    """How a run is hosted: what its durable side is, and how a host runs over it."""

    def new_store(self) -> Store:
        """The durable side of a new run."""
        ...

    def run(
        self,
        outside: OutsideWorld,
        options: HostOptions,
        clock_at: float,
        watcher: Watcher | None = None,
        patience: float | None = None,
    ) -> Hosted:
        """A new host over ``outside``, run to the end of its run, its failure, or ``patience``."""
        ...

    def restart_at(self, first_clock_at: float, store: Store) -> float:
        """When the host that takes over a dead one starts, on the first host's timeline."""
        ...


@dataclass(frozen=True)
class HostPlan:
    """How hosts of one run are made: on which hosting, and arranged as the options say."""

    hosting: Hosting
    options: HostOptions

    def run(
        self,
        outside: OutsideWorld,
        clock_at: float,
        watcher: Watcher | None = None,
        patience: float | None = None,
    ) -> Hosted:
        """A new host over ``outside``, run to the end of its run, its failure, or ``patience``."""
        return self.hosting.run(outside, self.options, clock_at, watcher, patience)

    def resuming(self, *, resumed: bool) -> HostPlan:
        """The plan for hosts the operator starts to resume the run (or, with none, to start it)."""
        return replace(self, options=replace(self.options, resumed=resumed))

    def restart_at(self, first_clock_at: float, store: Store) -> float:
        """When the host that takes over a dead one starts."""
        return self.hosting.restart_at(first_clock_at, store)


@dataclass(frozen=True)
class Hosted:
    """How one host ended, and when (on its own virtual clock)."""

    ended_at: float
    ended: Run | Exception
    core: CoreState | None
    """Core's state as the host last committed it, if it got as far as having one."""

    @property
    def finished(self) -> bool:
        """Whether the host ended the run itself: it returned with the run terminal."""
        return isinstance(self.ended, Run) and has_ended(self.ended.core)

    @property
    def how(self) -> str:
        """How the host ended, in a few words."""
        if isinstance(self.ended, Exception):
            return f"{type(self.ended).__name__}: {self.ended}"
        if self.ended.halted is not None:
            return f"{type(self.ended.halted).__name__}: {self.ended.halted}"
        return self.ended.core.run.status.value

    def run(self) -> Run:
        """The run the host ended, or the failure that ended it."""
        if isinstance(self.ended, Exception):
            raise self.ended
        return self.ended


class WrongStoreError(TypeError):
    """A host was given the durable side of another kind of host."""

    def __init__(self, store: object) -> None:
        """Name the store that does not fit."""
        super().__init__(f"this host cannot run over a {type(store).__name__}")
