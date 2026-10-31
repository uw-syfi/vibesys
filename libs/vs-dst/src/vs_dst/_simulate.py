"""Run a world's strategy to the end on the production shell, and from any crash of it."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from vs_core.api.testing import Budget, liveness
from vs_dst import _interrupt
from vs_dst._crash_points import CrashKind, CrashPoint, crash_point_policy
from vs_dst._effects import Effects
from vs_dst._host import (
    FIRST_HOST_STARTS_AT,
    Hosted,
    Hosting,
    HostPlan,
    ShellHosting,
    has_ended,
)
from vs_dst._interrupt import Expectation
from vs_dst._interruptions import Action, Interruption, Moment, choose
from vs_dst._snapshots import (
    NondeterministicWorldError,
    OutsideWorld,
    RecordingRecords,
    Snapshot,
    Snapshots,
)
from vs_runtime.api.testing import FakePublicationDelivery, HostOptions

if TYPE_CHECKING:
    from vs_core.api import CoreState, Decision, RunResultProposal, StrategyState
    from vs_core.api.testing import Journal, Violation
    from vs_dst._interrupt import Interrupted
    from vs_dst._world import World
    from vs_proto.api import Record
    from vs_runtime.api.core import DispatchCapExceededError, RunStalledError
    from vs_runtime.api.step_runner import StepLoopStalledError


@dataclass(frozen=True)
class Start:
    """Where a run's first host started within the whole run: all of it for a first host."""

    clock_at: float = FIRST_HOST_STARTS_AT
    commits: int = 0
    """How many commits earlier hosts made."""
    decisions: tuple[Decision, ...] = ()
    """What earlier hosts decided."""


class SimulatedRun:
    """One whole run, finished, and the means to ask what a crash of it would have done."""

    def __init__(
        self,
        hosted: Hosted,
        outside: OutsideWorld,
        plan: HostPlan,
        pristine: OutsideWorld,
        started: Start | None = None,
    ) -> None:
        """Wrap a finished ``hosted`` run over the ``outside`` world it left.

        ``pristine`` is the outside world as this run's first host found it, which crash cases
        are taken from; ``started`` says where in the whole run that was.
        """
        self._run = hosted.run()
        self._outside = outside
        self._plan = plan
        self._pristine = pristine
        self._started = Start() if started is None else started
        self._duration = hosted.ended_at - self._started.clock_at
        self._snapshots: dict[Interruption, Snapshot] = {}

    @property
    def core(self) -> CoreState:
        """Core's state when the run ended."""
        return self._run.core

    @property
    def result(self) -> RunResultProposal | None:
        """The run's result: its outcome, once it has one."""
        return self._run.core.run.result

    @property
    def world(self) -> World:
        """The world as this run left it: what it handed out and what it did to the far side.

        A recovered run has a world of its own (a copy of the outside world at its crash
        point), so a test reads what a crash left behind from it.
        """
        return self._outside.world

    @property
    def state(self) -> StrategyState:
        """The strategy state the shell last persisted."""
        return self._run.state

    @property
    def decisions(self) -> tuple[Decision, ...]:
        """Every decision any host of the run proposed, in order."""
        return (*self._started.decisions, *self._run.decisions)

    @property
    def journal(self) -> Journal:
        """The requests and observations of the host that ended the run (since its restart)."""
        return self._run.journal

    @property
    def finished(self) -> bool:
        """Whether the run reached its terminal state."""
        return has_ended(self._run.core)

    @property
    def halted(self) -> DispatchCapExceededError | RunStalledError | StepLoopStalledError | None:
        """Why the loop gave up on the run (dispatch cap, stall), or None."""
        return self._run.halted

    @property
    def liveness_violations(self) -> tuple[Violation, ...]:
        """The liveness invariants this run broke (`vs_core.testing.liveness`); none when live.

        The run's journal is checked against its final core state with the retry budget of the
        world's limits, expecting the run to end in its terminal state.
        """
        budget = Budget(retries=self._outside.world.setup.harness.limits.request_retries.limit)
        return tuple(liveness(self._run.journal, self._run.core, budget))

    @property
    def stopped(self) -> bool:
        """Whether an operator stop interrupted the run."""
        return self._run.stopped

    @property
    def log_records(self) -> tuple[Record, ...]:
        """The records of the run.log a step-loop run left, in order; none for a shell run."""
        store = self._outside.store
        return tuple(store.records) if isinstance(store, RecordingRecords) else ()

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
        return self.rerun_attempts == 0

    @property
    def rerun_attempts(self) -> int:
        """How many times an effect began again for a request whose effect had begun before."""
        executed = self._outside.effects.executed
        return len(executed) - len(set(executed))

    def crash_points(self, kind: CrashKind | None = None) -> tuple[CrashPoint, ...]:
        """The places a host of this run can die that the suite's policy tries.

        A sample on pull requests, every boundary under ``--depth=nightly``. ``kind``
        keeps one kind of point (a suite splits a long sweep that way). A run that started from a
        crash offers the commits its own hosts made, so it can be crashed again. Choosing costs
        nothing: the run only counted its commits.
        """
        first = self._started.commits
        points = tuple(
            replace(point, commit=point.commit + first)
            for point in crash_point_policy().choose(self.commits - first)
        )
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

        A recovered run can be crashed in turn: its points are those of its own hosts, and the
        run it results in reports the decisions of every host so far.
        """
        if not self._started.commits <= point.commit < self.commits:
            message = f"{point} is not a crash point of this run"
            raise ValueError(message)
        kill = Interruption.kill(point)
        snapshot = self._snapshot(kill, frozenset(map(Interruption.kill, self.crash_points())))
        outside = snapshot.outside.copy()
        clock_at = self._restart_at(snapshot)
        hosted = self._plan.run(outside, clock_at)
        started = Start(
            clock_at=clock_at,
            commits=snapshot.outside.store.commits,
            decisions=(*self._started.decisions, *self._run.decisions[: snapshot.decisions]),
        )
        return SimulatedRun(hosted, outside, self._plan, snapshot.outside, started)

    def interruptions(
        self, action: Action | None = None, moment: Moment | None = None
    ) -> tuple[Interruption, ...]:
        """The interruptions of this run that the suite's policy tries.

        A kill or a pause, just before a commit, just after one, or while an effect is in flight
        (`Moment`). A sample of each on pull requests, every one under ``--depth=nightly``.
        ``action`` and ``moment`` keep one class of them. Only a run no interruption has touched
        offers them: its numbering is the whole run's.
        """
        self._require_uninterrupted()
        points = choose(crash_point_policy(), self.commits, len(self._outside.effects.executed))
        return tuple(p for p in points if action in (None, p.action) and moment in (None, p.moment))

    def interrupt(self, point: Interruption) -> Interrupted:
        """What becomes of the run when ``point`` interrupts it and the operator resumes it.

        A kill starts from a copy of the outside world at ``point`` (the prefix is not run
        again); a pause runs the world from its start with the operator's pause planted at
        ``point``, since the host that is paused is alive and must be there to wind down. The
        operator then resumes the run, again if that host does not end it, up to
        `MAX_RESUMES` times. The result says how the run ended up (`Outcome`): never an
        exception, whatever a resume raises.

        The first kill runs the world once more from its start, copying the outside world at
        every kill the policy offers, as `rerun_with_crash` does.
        """
        self._require_uninterrupted()
        expected = Expectation(self.result, self._duration)
        if point.action is Action.PAUSE:
            return _interrupt.paused(
                self._plan, self._pristine, self._started.clock_at, point, expected
            )
        snapshot = self._snapshot(point, frozenset(self.interruptions(Action.KILL)))
        outside = snapshot.outside.copy()
        return _interrupt.killed(self._plan, outside, self._restart_at(snapshot), point, expected)

    def _require_uninterrupted(self) -> None:
        if self._started.commits:
            message = "only a run no interruption has touched offers interruptions"
            raise ValueError(message)

    def _restart_at(self, snapshot: Snapshot) -> float:
        """When the host that takes over a dead one starts, as its hosting says."""
        return self._plan.restart_at(self._started.clock_at, snapshot.outside.store)

    def _snapshot(self, point: Interruption, offered: frozenset[Interruption]) -> Snapshot:
        """The outside world at the kill ``point``.

        The first call takes the copies of every ``offered`` kill with it.
        """
        if point not in self._snapshots:
            self._take_snapshots(offered if not self._snapshots else frozenset(), point)
        return self._snapshots[point]

    def _take_snapshots(self, wanted: frozenset[Interruption], point: Interruption) -> None:
        """Run the world again from its start, copying the outside world at the ``wanted`` kills."""
        outside = self._pristine.copy()
        snapshots = Snapshots(outside, wanted | {point})
        self._plan.run(outside, self._started.clock_at, snapshots).run()
        if outside.store.commits != self.commits:
            raise NondeterministicWorldError(outside.world, self.commits, outside.store.commits)
        self._snapshots.update(snapshots.by_point)


def simulate(world: World, hosting: Hosting | None = None) -> SimulatedRun:
    """Run ``world``'s strategy to the end of its run on a host, and keep the means to crash it.

    ``hosting`` says which host: the production shell (``CoreRuntime``) by default, with its
    loop, lease, fence, clock, durable commit and owner-event queue on a virtual clock and an
    in-memory store, or the step loop (`StepLoopHosting`) over an in-memory run.log. The run
    copies nothing while it goes (it counts its commits), so a test that never crashes it pays
    one copy of the initial world; `rerun_with_crash` runs the world once more to take the copies
    a crash sweep needs. A world that cannot be deep-copied raises `WorldNotCopyableError`.

    How the host is arranged (its concurrency, a run-control channel) is the world's
    `RunSetup`; a `HostedWorld` also hears each host's shell and clock.
    """
    hosting = ShellHosting() if hosting is None else hosting
    setup = world.setup
    options = HostOptions(max_concurrent=setup.max_concurrent, controlled=setup.controlled)
    outside = OutsideWorld(hosting.new_store(), FakePublicationDelivery(), Effects(), world)
    pristine = outside.copy()  # also fails now, naming the world, if it cannot be copied
    plan = HostPlan(hosting, options)
    return SimulatedRun(plan.run(outside, FIRST_HOST_STARTS_AT), outside, plan, pristine)
