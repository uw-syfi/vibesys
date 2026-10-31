"""One host of a run: a new shell and strategy instance over the outside world, run until it ends."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from vs_core.api import ContractError, FlowCodeChanged, RunStatus
from vs_dst._effects import Effects, RecoverableExecutors
from vs_dst._interruptions import Moment
from vs_dst._world import HostedWorld
from vs_runtime.api.core import (
    DispatchCapExceededError,
    RunStalledError,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RunStopped
from vs_runtime.api.testing import Run, scripted_host
from vs_sim.api.testing import run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, OperationRegistry, Request, SchemaRef
    from vs_core.api.testing import Journal
    from vs_dst._snapshots import OutsideWorld, Watcher
    from vs_dst._world import Enact, World
    from vs_runtime.api.core import CoreRuntime, ExecutionResult
    from vs_runtime.api.testing import HostOptions, Script, ScriptedHost, TranslatorFactory
    from vs_sim.api.testing import VirtualClock

FIRST_HOST_STARTS_AT = 1.0
"""The virtual time the first host starts at."""

PATIENCE_STEP = 1.0
"""How often a host that is waited on only so long looks at the clock, in virtual seconds."""

RUN_FAILURES = (ContractError, FlowCodeChanged, RuntimeError)
"""What a host that fails by its own contract raises: core refusing a transition, a flow that
cannot be restored from its journal, the runtime refusing to commit. Anything else is a defect
in the test, and propagates."""


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
        return isinstance(self.ended, Run) and self.ended.core.run.status == RunStatus.TERMINAL

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


def run_host(
    outside: OutsideWorld,
    options: HostOptions,
    clock_at: float,
    watcher: Watcher | None = None,
    patience: float | None = None,
) -> Hosted:
    """A new host over ``outside``, run to the end of its run (or its failure).

    A host that ends on its own (it was not killed) gives its lease back, as the process does
    when it exits, so its successor need not wait for it to expire. With a ``patience`` the
    host is given up on that many virtual seconds after it starts, leaving its run open as a
    host that is stopped does.
    """
    world = outside.world
    setup = world.setup
    enactment = _Enactment(world)
    host = scripted_host(
        setup.new_launch(),
        world,
        setup.harness,
        setup.schemas,
        replace(
            options,
            translator=_translator(outside.effects, enactment, watcher),
            attach=enactment.attach,
        ),
        store=outside.store,
        delivery=outside.delivery,
        clock_at=clock_at,
    )
    if watcher is not None:
        watcher.watch(outside.store, host)
    ended: Run | Exception
    try:
        ended = host.run() if patience is None else _run_within(host, patience)
    except RUN_FAILURES as failure:
        ended = failure
    now = host.clock.now()
    host.shell.release_lease(now_at=now)
    return Hosted(now, ended, _committed(host))


def _run_within(host: ScriptedHost, patience: float) -> Run:
    """What ``host.run()`` does, except that the loop is cancelled after ``patience`` seconds."""
    start_core(host.host, host.config)
    halted = None
    stopped = False
    try:
        run_virtual(host.clock, _until_given_up(host, patience))
    except RunStopped:
        stopped = True
    except (DispatchCapExceededError, RunStalledError) as error:
        halted = error
    record = host.shell.record
    return Run(
        core=record.envelope.core,
        state=record.envelope.strategy,
        decisions=host.decisions,
        journal=host.journal,
        halted=halted,
        stopped=stopped,
    )


async def _until_given_up(host: ScriptedHost, patience: float) -> None:
    loop = asyncio.ensure_future(drive_core(host.host, host.config))
    give_up_at = host.clock.now() + patience
    while not loop.done() and host.clock.now() < give_up_at:
        await host.clock.sleep(PATIENCE_STEP)
    if loop.done():
        await loop
        return
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)


def _committed(host: ScriptedHost) -> CoreState | None:
    try:
        return host.shell.record.envelope.core
    except RuntimeError:  # the host failed before its shell started: it committed nothing
        return None


class _Enactment:
    """How one host's executed requests are enacted: the world's say, once the host exists."""

    def __init__(self, world: World) -> None:
        self._world = world
        self._enact: Enact | None = None

    def attach(self, shell: CoreRuntime[Any], clock: VirtualClock) -> None:
        """Let a `HostedWorld` bind this host's shell and clock."""
        if isinstance(self._world, HostedWorld):
            self._enact = self._world.attach(shell, clock)

    async def __call__(self, request: Request, result: ExecutionResult) -> ExecutionResult:
        """The result the shell is told for ``request``."""
        return result if self._enact is None else await self._enact(request, result)


def _translator(
    effects: Effects, enactment: _Enactment, watcher: Watcher | None
) -> TranslatorFactory:
    """Executors that record into, and answer inspections from, the shared ``effects`` ledger.

    What each request does on the host (see `HostedWorld`) passes through ``enactment``, and
    the ``watcher`` hears each effect as it begins.
    """

    def build(
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal,
    ) -> RecoverableExecutors:
        class HostExecutors(RecoverableExecutors):
            def _began(self, index: int) -> None:
                if watcher is not None:
                    watcher.reached(Moment.IN_FLIGHT, index)

            async def _enacted(self, request: Request, result: ExecutionResult) -> ExecutionResult:
                return await enactment(request, result)

        return HostExecutors(script, core, registry, schemas, journal, effects)

    return build
