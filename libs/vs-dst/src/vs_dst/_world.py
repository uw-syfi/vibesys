"""What a test builds: the strategy that runs and the far side of its run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, Request, SchemaRef
    from vs_core.api.testing import Answer, Harness
    from vs_runtime.api.testing import Launch


@dataclass(frozen=True)
class RunSetup:
    """Which search runs and what hosts it. Immutable, so a snapshot of a world shares it."""

    new_launch: Callable[[], Launch[Any]]
    """A fresh search with its config; every call builds an equal one."""
    harness: Harness
    """Registry, run facts, limits, envelope type and evidence requirements of the run."""
    schemas: Mapping[SchemaRef, type[BaseModel]]
    """The reply types the run's agent turns answer."""
    upgrade: Callable[[], Launch[Any]] | None = None
    """The search the code has become, for a world that tests resuming a run on changed code.

    A host that resumes a run (the operator's Resume after a pause or a crash) runs this search
    instead of ``new_launch``'s; the first host of a run never does. ``None``: the code does not
    change."""


class World(Protocol):
    """The far side of a run: executors that answer every request, and what runs against them.

    A world is the outside world's position as well as its script: replies it has handed out
    are consumed, so a copy taken mid-run continues from where the run stood. `simulate` copies
    it with `copy.deepcopy` (once per commit), so everything a world holds must be copyable
    plain data, apart from its `setup`, which is shared and never copied.
    """

    @property
    def setup(self) -> RunSetup:
        """The strategy under test and its host."""
        ...

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        ...


@runtime_checkable
class HoldingWorld(Protocol):
    """A world whose far side can hold a request forever: a job queued that nothing ever starts.

    The host honours it: the effect of a request the world holds began, never ends by itself and
    seals nothing, and it ends only when the loop cancels it, as a job held pending in a
    scheduler's queue ends only by a cancel.
    """

    def holds(self, request: Request) -> bool:
        """Whether the effect of ``request`` stays pending until the loop cancels it."""
        ...
