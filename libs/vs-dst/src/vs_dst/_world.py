"""What a test builds: the strategy that runs and the far side of its run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, Request, SchemaRef, Strategy
    from vs_core.api.testing import Answer, Harness


@dataclass(frozen=True)
class RunSetup:
    """Which strategy runs and what hosts it. Immutable, so a snapshot of a world shares it."""

    new_strategy: Callable[[], Strategy[Any]]
    """A fresh strategy instance over its initial state; every call builds an equal one."""
    harness: Harness
    """Registry, run facts, limits, envelope type and evidence requirements of the run."""
    schemas: Mapping[SchemaRef, type[BaseModel]]
    """The reply types the run's agent turns answer."""


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
