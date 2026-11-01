"""What a test builds: the strategy that runs and the far side of its run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreEvent, CoreState, Request, SchemaRef, Transition
    from vs_core.api.testing import Answer, Harness
    from vs_runtime.api.testing import Launch
    from vs_sim.api import Clock


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


@runtime_checkable
class ClearFailingWorld(Protocol):
    """A world whose far side cannot be reached to clear a request: a Docker daemon that is down.

    The host honours it: the runner's ``clear`` of a request the world says it cannot clear
    raises ``ClearUnavailableError``, as the real runners' do when their backend is unreachable,
    so the loop retries it with backoff and records no result over it. It is asked at every
    ``clear``, which a step takes only when work of it may exist (after a kill, or an attempt
    that ended without a result).
    """

    def clear_fails(self, request: Request) -> bool:
        """Whether ``clear`` of ``request`` cannot reach its backend now."""
        ...


class Admission(Protocol):
    """The run as a service the host runs beside the loop sees it: commit an input, then answer.

    This is how a tool an agent calls during its turn reaches core, as the production host's
    evaluation bridge does: the input is in the run.log before the call returns.
    """

    @property
    def admitted_core(self) -> CoreState:
        """The committed core, including every input admitted so far."""
        ...

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """Commit core's step of ``event`` before returning; a refusal commits nothing."""
        ...


class Serving(Protocol):
    """What a world runs inside one host process: its memory, which ends with the host."""

    async def during(self, request: Request, core: CoreState) -> None:
        """Act while the effect of ``request`` is in flight (an agent's tool calls in its turn).

        The effect has begun and nothing of it is sealed, so a host that dies here leaves the
        calls made and the effect to run again, as a real turn does.
        """
        ...

    async def close(self) -> None:
        """The host's loop has returned, or been cancelled: release what the service holds."""
        ...


@runtime_checkable
class ServingWorld(Protocol):
    """A world that serves each host process, through the run's admission of inputs.

    The world stays the outside world's position (plain data, copied with a crash); what it
    serves is the host's own and is made again by every host, from that position.
    """

    def serve(self, admission: Admission, clock: Clock) -> Serving:
        """The service of a new host: it admits inputs through ``admission``, on ``clock``."""
        ...
