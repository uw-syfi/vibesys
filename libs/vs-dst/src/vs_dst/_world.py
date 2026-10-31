"""What a test builds: the strategy that runs and the far side of its run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, Request, SchemaRef
    from vs_core.api.testing import Answer, Harness
    from vs_runtime.api.core import CoreRuntime, ExecutionResult
    from vs_runtime.api.testing import Launch
    from vs_sim.api.testing import VirtualClock


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
    max_concurrent: int = 1
    """How many requests the loop executes at once."""
    controlled: bool = False
    """Whether the loop has an idle run-control channel, as a product run has, so it looks at the
    shell every control-poll interval while requests run (a world whose agents act on the shell
    while their turn runs needs it)."""


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


type Enact = Callable[[Request, ExecutionResult], Awaitable[ExecutionResult]]
"""What happens around an executed request: given the request and the result its executor
produced, the result the shell is told (after the world's own side effects on the host, such as
an agent calling a tool server while its turn runs)."""


@runtime_checkable
class HostedWorld(Protocol):
    """A world that also acts on the host: it hears each host's shell and clock.

    `attach` is called once per host, so a restarted host gets its own, once its `CoreRuntime`
    and clock exist and before the loop starts. Everything that belongs to a host (a tool server
    bound to the shell, the clock) is created there and kept in the returned `Enact`, never in
    the world, because the world is copied at crash points and a host is not.
    """

    def attach(self, shell: CoreRuntime[Any], clock: VirtualClock) -> Enact | None:
        """Bind this host's ``shell`` and ``clock``; return how its executed requests are enacted."""
        ...
