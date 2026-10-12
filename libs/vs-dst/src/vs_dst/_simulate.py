"""Run a world's strategy to the end on the production shell, and from any crash of it."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from vs_core.api import RunStatus
from vs_core.api.testing import Budget, liveness
from vs_dst._crash_points import CrashKind, CrashPoint, crash_point_policy
from vs_dst._effects import Effects, RecoverableExecutors
from vs_dst._snapshots import (
    NondeterministicWorldError,
    OutsideWorld,
    RecordingStore,
    Snapshot,
    Snapshots,
)
from vs_dst._world import HostedWorld
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

    from vs_core.api import (
        CoreState,
        Decision,
        OperationRegistry,
        Request,
        RunResultProposal,
        SchemaRef,
        StrategyState,
    )
    from vs_core.api.testing import Journal, Violation
    from vs_dst._world import Enact, World
    from vs_runtime.api.core import (
        CoreRuntime,
        DispatchCapExceededError,
        ExecutionResult,
        RunStalledError,
    )
    from vs_runtime.api.testing import Script
    from vs_sim.api.testing import VirtualClock

FIRST_HOST_STARTS_AT = 1.0
"""The virtual time the first host starts at."""


class SimulatedRun:
    """One whole run, finished, and the means to ask what a crash of it would have done."""

    def __init__(
        self,
        run: Run,
        outside: OutsideWorld,
        options: HostOptions,
        pristine: OutsideWorld | None,
        earlier_decisions: tuple[Decision, ...] = (),
    ) -> None:
        """Wrap a finished host's ``run`` over the ``outside`` world it left.

        ``pristine`` is a copy of the outside world from before the run, which crash cases are
        taken from; a recovered run has none.
        """
        self._run = run
        self._outside = outside
        self._options = options
        self._pristine = pristine
        self._earlier_decisions = earlier_decisions
        self._snapshots: dict[CrashPoint, Snapshot] = {}

    @property
    def core(self) -> CoreState:
        """Core's state when the run ended."""
        return self._run.core

    @property
    def result(self) -> RunResultProposal | None:
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
    def liveness_violations(self) -> tuple[Violation, ...]:
        """The liveness invariants this run broke (`vs_core.testing.liveness`); none when live.

        The run's journal is checked against its final core state with the retry budget of the
        world's limits, expecting the run to end in its terminal state.
        """
        budget = Budget(retries=self._outside.world.setup.harness.limits.max_retries)
        return tuple(liveness(self._run.journal, self._run.core, budget))

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
        crash has none of its own. Choosing costs nothing: the run only counted its commits.
        """
        if self._pristine is None:
            return ()
        points = crash_point_policy().choose(self.commits)
        return points if kind is None else tuple(p for p in points if p.kind is kind)

    def rerun_with_crash(self, point: CrashPoint) -> SimulatedRun:
        """The run that results when the host dies at ``point`` and a new host takes over.

        The new host (a new `CoreRuntime` over a new strategy instance) starts on a copy of the
        outside world as it stood at ``point``, after the dead host's lease has expired, and runs
        to the end. The prefix of the run is not run again.

        The first call runs the world once more from its start and copies the outside world at
        every point `crash_points()` offers (it raises `NondeterministicWorldError` if that run
        makes a different number of commits). A point the policy did not offer, but that is
        within the run, gets a run of its own; a point outside the run raises `ValueError`.
        """
        if self._pristine is None or not 0 <= point.commit < self.commits:
            message = f"{point} is not a crash point of this run"
            raise ValueError(message)
        if point not in self._snapshots:
            offered = frozenset(self.crash_points()) if not self._snapshots else frozenset()
            self._take_snapshots(self._pristine, offered | {point})
        snapshot = self._snapshots[point]
        outside = snapshot.outside.copy()
        # The next host starts after the dead one's lease has expired.
        run = _run_host(outside, self._options, FIRST_HOST_STARTS_AT + LEASE + 1, None)
        return SimulatedRun(
            run,
            outside,
            self._options,
            pristine=None,
            earlier_decisions=tuple(self._run.decisions[: snapshot.decisions]),
        )

    def _take_snapshots(self, pristine: OutsideWorld, wanted: frozenset[CrashPoint]) -> None:
        """Run the world again from its start, copying the outside world at the ``wanted`` points."""
        outside = pristine.copy()
        snapshots = Snapshots(outside, wanted)
        _run_host(outside, self._options, FIRST_HOST_STARTS_AT, snapshots)
        if outside.store.commits != self.commits:
            raise NondeterministicWorldError(outside.world, self.commits, outside.store.commits)
        self._snapshots.update(snapshots.by_point)


def simulate(world: World) -> SimulatedRun:
    """Run ``world``'s strategy to the end of its run on the production shell and loop.

    The loop, the lease, the fence, the clock, the durable commit and the owner-event queue
    are the real `vs_runtime` ones, on a virtual clock and an in-memory store. The run copies
    nothing while it goes (it counts its commits), so a test that never crashes it pays one copy
    of the initial world; `rerun_with_crash` runs the world once more to take the copies a crash
    sweep needs. A world that cannot be deep-copied raises `WorldNotCopyableError`.

    How the host is arranged (its concurrency, a run-control channel) is the world's
    `RunSetup`; a `HostedWorld` also hears each host's shell and clock.
    """
    setup = world.setup
    options = HostOptions(max_concurrent=setup.max_concurrent, controlled=setup.controlled)
    store = RecordingStore()
    outside = OutsideWorld(store, FakePublicationDelivery(store), Effects(), world)
    pristine = outside.copy()  # also fails now, naming the world, if it cannot be copied
    run = _run_host(outside, options, FIRST_HOST_STARTS_AT, None)
    return SimulatedRun(run, outside, options, pristine)


def _run_host(
    outside: OutsideWorld, options: HostOptions, clock_at: float, snapshots: Snapshots | None
) -> Run:
    """A new host (shell, strategy instance, executors) over ``outside``, run to the end."""
    world = outside.world
    setup = world.setup
    enactment = _Enactment(world)
    host = scripted_host(
        setup.new_strategy(),
        world,
        setup.harness,
        setup.schemas,
        replace(
            options, translator=_translator(outside.effects, enactment), attach=enactment.attach
        ),
        store=outside.store,
        delivery=outside.delivery,
        clock_at=clock_at,
    )
    if snapshots is not None:
        snapshots.watch(outside.store, host.decisions)
    return host.run()


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


def _translator(effects: Effects, enactment: _Enactment) -> TranslatorFactory:
    """Executors that record into, and answer inspections from, the shared ``effects`` ledger.

    What each request does on the host (see `HostedWorld`) passes through ``enactment``.
    """

    def build(
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal,
    ) -> RecoverableExecutors:
        class HostExecutors(RecoverableExecutors):
            async def _enacted(self, request: Request, result: ExecutionResult) -> ExecutionResult:
                return await enactment(request, result)

        return HostExecutors(script, core, registry, schemas, journal, effects)

    return build
