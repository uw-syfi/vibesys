"""One contract every executor that creates and releases passes: no effect after "nothing held".

An executor pair has a *setup* request that creates something (a session binding, a provider
turn, a workspace) and a *release* request that answers whether anything is held and releases
it (CloseSession, CancelTurn, DiscardWorkspace). Both go through the effect fence
(``vs_runtime._effect_fence``). The shared test in
``libs/vs-runtime/tests/test_effect_fence_contract.py`` runs every registered pair through
seeded schedules under ``vs_sim``: deliveries of the setup (redelivered, and under a second
identity that arrives late), of the release, concurrent deliveries on one host, and host
crashes at every durable-write boundary followed by a newer host. It checks:

* after a release answers "released" (nothing held, or what was held is gone), no later step
  of any delivery creates anything, and nothing is held at the answer;
* a host that crashed between creating and answering leaves the creation visible: a later
  release finds it and releases it, so nothing stays held behind a "released" answer;
* a release delivered on a newer host after every other delivery ends reaches a terminal
  answer, so the fence never strands a release.

A new executor implements ``FencePair`` and gets all of it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, cast

from vs_runtime.api.executor_contracts import ProcessKilledError

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from pydantic import BaseModel

    from vs_core.api import RequestBase
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import ExecutionResult
    from vs_sim.api import BlockingRunner


class HostLife:
    """One simulated process: it dies at a chosen durable-write boundary and stays dead.

    Boundary ``2 * i`` is just before write ``i`` and ``2 * i + 1`` just after it, counted
    over every delivery the host runs. A dead host writes nothing and runs no blocking call
    again: each attempt raises ``ProcessKilledError``, as if the process were gone.
    """

    def __init__(self, crash_at: int | None) -> None:
        """Die at boundary *crash_at* (never when ``None``)."""
        self._crash_at = crash_at
        self.writes = 0
        self.dead = False
        self._reaped = False

    @property
    def stopped(self) -> bool:
        """Whether the host died and its resources were not reaped yet."""
        return self.dead and not self._reaped

    def reap(self) -> None:
        """Let the harness close what the dead process held, as the OS does when it exits.

        Called once every delivery ended, so nothing the process ran can act any more.
        """
        self._reaped = True

    def boundary(self, index: int) -> None:
        """Pass a boundary, dying if it is the chosen one or the host already died."""
        if self.dead or self._crash_at == index:
            self.dead = True
            raise ProcessKilledError

    def namespace(self, real: StateNamespace) -> StateNamespace:
        """*real*, with every write passing this host's boundaries."""
        return cast("StateNamespace", _MortalNamespace(real, self))

    def runner(self, real: BlockingRunner) -> BlockingRunner:
        """*real*, refusing every blocking call once the host died."""
        return _MortalRunner(real, self)


class _MortalNamespace:
    def __init__(self, real: StateNamespace, life: HostLife) -> None:
        self._real = real
        self._life = life

    def _write[T](self, write: Callable[[], T]) -> T:
        self._life.boundary(2 * self._life.writes)
        result = write()
        self._life.boundary(2 * self._life.writes + 1)
        self._life.writes += 1
        return result

    def save(self, relative_path: str, model: BaseModel) -> None:
        self._write(lambda: self._real.save(relative_path, model))

    def write_bytes(self, relative_path: str, contents: bytes) -> None:
        self._write(lambda: self._real.write_bytes(relative_path, contents))

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class _MortalRunner:
    def __init__(self, real: BlockingRunner, life: HostLife) -> None:
        self._real = real
        self._life = life

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        if self._life.stopped:
            raise ProcessKilledError

        def guarded() -> T:
            if self._life.stopped:
                raise ProcessKilledError
            return function(*args, **kwargs)

        return await self._real.run(guarded)


class FenceHost(Protocol):
    """One host process of a pair's world: a freshly built executor over the shared disk."""

    async def execute(self, request: RequestBase) -> ExecutionResult:
        """Run one delivery of *request* on this host."""
        ...


class FenceWorld(Protocol):
    """One pair's durable world: the requests, the hosts that run them, and what exists."""

    async def prepare(self, host: FenceHost) -> None:
        """Seed what the setup needs (an ensured session for a turn) on a host that never dies."""
        ...

    def setup(self, identity: str) -> RequestBase:
        """A request that creates the pair's resource, under request identity *identity*."""
        ...

    def release(self, identity: str) -> RequestBase:
        """A request that releases the pair's resource, under request identity *identity*."""
        ...

    def host(self, epoch: int, life: HostLife, blocking: BlockingRunner) -> FenceHost:
        """A host with *epoch*: its store writes through ``life.namespace``.

        *blocking* already refuses calls once the host died; the host runs every blocking
        call on it, its executor's and its owners' alike.
        """
        ...

    def created(self) -> int:
        """How many times anything was created so far; never decreases."""
        ...

    def held(self) -> int:
        """How many created things are held now (not yet released)."""
        ...


class FencePair(Protocol):
    """A registered setup/release pair and a fresh world to run it in."""

    name: str

    def world(self) -> AbstractAsyncContextManager[FenceWorld]:
        """A fresh world in its own temporary directory, with any prerequisite seeded."""
        ...


type DeliveryKind = Literal["setup", "late-setup", "release"]


@dataclass(frozen=True)
class HostRun:
    """Deliveries one host runs concurrently, and the write boundary it dies at, if any.

    Each run is a new host with a newer epoch, as after a restart: a host that died is never
    heard from again.
    """

    deliveries: tuple[DeliveryKind, ...]
    crash_at: int | None = None


@dataclass
class ScheduleReport:
    """What a schedule did: the contract's violations, and the creation count at each release."""

    violations: list[str] = field(default_factory=list)
    created_at_release: list[int] = field(default_factory=list)
    """``FenceWorld.created`` when each "released" answer came back, in order."""

    def released(self, world: FenceWorld) -> None:
        """Note a "released" answer: nothing may be held now, and nothing created later."""
        self.created_at_release.append(world.created())
        if held := world.held():
            self.violations.append(f"release answered released while {held} thing(s) are held")

    def ended(self, world: FenceWorld, final: ExecutionResult) -> None:
        """Check the final release's answer and what exists once every delivery ended."""
        observed = final.observation.observation
        if not observed.terminal:
            self.violations.append(
                "a release on a newer host after every delivery ended stayed Unknown: "
                f"{observed.diagnostic}"
            )
        elif observed.released:
            self.released(world)
        if not self.created_at_release:
            return
        if (late := world.created() - self.created_at_release[0]) != 0:
            self.violations.append(f"created {late} thing(s) after a release answered released")
        if held := world.held():
            self.violations.append(f"{held} thing(s) held after the release ended")


def _identity(kind: DeliveryKind) -> str:
    return {"setup": "a", "late-setup": "b", "release": "r"}[kind]


async def run_schedule(
    world: FenceWorld, runs: tuple[HostRun, ...], blocking: BlockingRunner
) -> ScheduleReport:
    """Run *runs* in order, one host each with a rising epoch, and check the contract."""
    report = ScheduleReport()

    async def deliver(host: FenceHost, kind: DeliveryKind) -> None:
        identity = _identity(kind)
        request = world.release(identity) if kind == "release" else world.setup(identity)
        try:
            result = await host.execute(request)
        except ProcessKilledError:
            return
        observed = result.observation.observation
        if kind == "release" and observed.terminal and observed.released:
            report.released(world)

    await world.prepare(world.host(0, HostLife(None), blocking))
    lives = [HostLife(run.crash_at) for run in runs]
    for epoch, (run, life) in enumerate(zip(runs, lives, strict=True), start=1):
        host = world.host(epoch, life, life.runner(blocking))
        await asyncio.gather(*(deliver(host, kind) for kind in run.deliveries))
    for life in lives:
        life.reap()
    final = world.host(len(runs) + 1, HostLife(None), blocking)
    report.ended(world, await final.execute(world.release(_identity("release"))))
    return report


__all__ = [
    "DeliveryKind",
    "FenceHost",
    "FencePair",
    "FenceWorld",
    "HostLife",
    "HostRun",
    "ScheduleReport",
    "run_schedule",
]
