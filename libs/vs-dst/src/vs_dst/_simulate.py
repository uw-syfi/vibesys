"""Run a world's strategy to the end on the production shell, and from any crash of it."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from vs_core.api import RunStatus
from vs_dst._crash_points import CrashKind, CrashPoint, crash_point_policy
from vs_dst._effects import Effects, RecoverableExecutors
from vs_dst._snapshots import OutsideWorld, RecordingStore, Snapshots
from vs_runtime.api.testing import (
    LEASE,
    FakePublicationDelivery,
    HostOptions,
    Run,
    TranslatorFactory,
    scripted_host,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, Decision, OperationRegistry, SchemaRef, StrategyState
    from vs_core.api.testing import Journal
    from vs_dst._world import World
    from vs_runtime.api.core import DispatchCapExceededError, RunStalledError
    from vs_runtime.api.testing import Script

FIRST_HOST_STARTS_AT = 1.0
"""The virtual time the first host starts at."""


class SimulatedRun:
    """One whole run, finished, and the means to ask what a crash of it would have done."""

    def __init__(
        self,
        run: Run,
        outside: OutsideWorld,
        options: HostOptions,
        snapshots: Snapshots | None,
        earlier_decisions: tuple[Decision, ...] = (),
    ) -> None:
        """Wrap a finished host's ``run`` over the ``outside`` world it left."""
        self._run = run
        self._outside = outside
        self._options = options
        self._snapshots = snapshots
        self._earlier_decisions = earlier_decisions

    @property
    def core(self) -> CoreState:
        """Core's state when the run ended."""
        return self._run.core

    @property
    def result(self) -> object:
        """The run's result: its outcome, once it has one."""
        return self._run.core.run.result

    @property
    def state(self) -> StrategyState:
        """The strategy state the shell last persisted."""
        return self._run.state

    @property
    def decisions(self) -> tuple[Decision, ...]:
        """Every decision any host of the run proposed, in order."""
        return (*self._earlier_decisions, *self._run.decisions)

    @property
    def journal(self) -> Journal:
        """The requests and observations of the host that ended the run (since its restart)."""
        return self._run.journal

    @property
    def finished(self) -> bool:
        """Whether the run reached its terminal state."""
        return self._run.core.run.status == RunStatus.TERMINAL

    @property
    def halted(self) -> DispatchCapExceededError | RunStalledError | None:
        """Why the loop gave up on the run (dispatch cap, stall), or None."""
        return self._run.halted

    @property
    def stopped(self) -> bool:
        """Whether an operator stop interrupted the run."""
        return self._run.stopped

    @property
    def commits(self) -> int:
        """How many commits the store was asked for, over all hosts of the run."""
        return self._outside.store.commits

    @property
    def executed_effects(self) -> tuple[str, ...]:
        """The identity of every request whose effect ran, in order, across all hosts."""
        return tuple(self._outside.effects.executed)

    def no_effect_ran_twice(self) -> bool:
        """Whether no request identity had its effect run more than once."""
        executed = self._outside.effects.executed
        return len(executed) == len(set(executed))

    def crash_points(self, kind: CrashKind | None = None) -> tuple[CrashPoint, ...]:
        """The places a host of this run can die that the suite's policy tries.

        A sample on pull requests, every boundary under ``--dst-crash-points=all``. ``kind``
        keeps one kind of point (a suite splits a long sweep that way). A run that started from a
        crash has none of its own.
        """
        if self._snapshots is None:
            return ()
        points = crash_point_policy().choose(self.commits)
        return points if kind is None else tuple(p for p in points if p.kind is kind)

    def rerun_with_crash(self, point: CrashPoint) -> SimulatedRun:
        """The run that results when the host dies at ``point`` and a new host takes over.

        The new host (a new `CoreRuntime` over a new strategy instance) starts on a copy of the
        outside world as it stood at ``point``, after the dead host's lease has expired, and runs
        to the end. The prefix of the run is not run again.
        """
        if self._snapshots is None or point not in self._snapshots.by_point:
            message = f"{point} is not a crash point of this run"
            raise ValueError(message)
        snapshot = self._snapshots.by_point[point]
        outside = snapshot.outside.copy()
        # The next host starts after the dead one's lease has expired.
        run = _run_host(outside, self._options, FIRST_HOST_STARTS_AT + LEASE + 1, None)
        return SimulatedRun(
            run,
            outside,
            self._options,
            snapshots=None,
            earlier_decisions=tuple(self._run.decisions[: snapshot.decisions]),
        )


def simulate(world: World, *, max_concurrent: int = 1, controlled: bool = False) -> SimulatedRun:
    """Run ``world``'s strategy to the end of its run on the production shell and loop.

    The loop, the lease, the fence, the clock, the durable commit and the owner-event queue
    are the real `vs_runtime` ones, on a virtual clock and an in-memory store. While it runs,
    the outside world is copied just before and just after every commit, which is what makes
    `rerun_with_crash` cheap. A world that cannot be deep-copied raises `WorldNotCopyableError`.

    ``max_concurrent`` is how many requests the loop executes at once; ``controlled`` gives it an
    idle run-control channel, as a product run has.
    """
    options = HostOptions(max_concurrent=max_concurrent, controlled=controlled)
    store = RecordingStore()
    outside = OutsideWorld(store, FakePublicationDelivery(store), Effects(), world)
    outside.copy()  # fail now, naming the world, rather than inside the first commit of a loop
    snapshots = Snapshots(outside)
    run = _run_host(outside, options, FIRST_HOST_STARTS_AT, snapshots)
    return SimulatedRun(run, outside, options, snapshots)


def _run_host(
    outside: OutsideWorld, options: HostOptions, clock_at: float, snapshots: Snapshots | None
) -> Run:
    """A new host (shell, strategy instance, executors) over ``outside``, run to the end."""
    setup = outside.world.setup
    host = scripted_host(
        setup.new_strategy(),
        outside.world,
        setup.harness,
        setup.schemas,
        replace(options, translator=_translator(outside.effects)),
        store=outside.store,
        delivery=outside.delivery,
        clock_at=clock_at,
    )
    if snapshots is not None:
        snapshots.watch(outside.store, host.decisions)
    return host.run()


def _translator(effects: Effects) -> TranslatorFactory:
    """Executors that record into, and answer inspections from, the shared ``effects`` ledger."""

    def build(
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal,
    ) -> RecoverableExecutors:
        return RecoverableExecutors(script, core, registry, schemas, journal, effects)

    return build
