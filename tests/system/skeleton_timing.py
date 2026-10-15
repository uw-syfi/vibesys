"""Time on the skeleton world: agent turns that last virtual seconds, stops and held jobs.

The skeleton world answers a turn at once, so a lease that lapses, a stop that waits for a
turn, or two turns that never overlap cannot show. This module adds the pieces the timing
tests of ``tests/system/timing`` need and nothing of any search's logic:

- ``TurnTimeline`` and ``TimedTurns``: the real executors, except that an agent turn first
  lasts a duration on the virtual clock. A ``CancelTurn`` ends that wait early (as the provider
  turn is interrupted); ``obeys_cancel=False`` is a turn that ignores it.
- ``HoldingCluster``: the Fake cluster, with jobs from the Nth submission on that never end by
  themselves, and a record of when each was submitted.
- ``StaggeredStrategy``: a scripted search whose workers each run one turn in a session of
  their own and measure what the turn produced the moment it ends.
- ``open_timed_world`` and ``play``: the world with those pieces, and the production loop
  driven on a ``VirtualClock`` with a stop requested when a condition holds and the host's
  grace bound that cancels the loop.

Everything waits through the virtual clock, so a run of minutes takes milliseconds and the
same inputs always give the same timeline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.skeleton_strategy import (
    IMPLEMENTER_ROLE,
    OPERATIONS,
    Implementation,
    Prompt,
    SkeletonStrategy,
    measurement,
)
from tests.support.skeleton_world import LEASE, World, open_skeleton_world

import vs_core.api as core
from vs_core.api.search import INPUT, Failed, Flow, NotOpened, NoWinner, TurnFinished, search
from vs_core.api.testing import sample_limits
from vs_runtime.api.core import (
    CoreRunHost,
    RequestExecutors,
    RunControlBridge,
    RunLoopConfig,
    RunOutcome,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RuntimeRunControlChannel
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster
from vs_sim.api.testing import VirtualClock, loop_threads, run_virtual
from vs_slurm.api import SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_core.api import CoreState, Request
    from vs_runtime.api.core import (
        CoreRuntimeBindings,
        ExecutionContext,
        ExecutionOutcome,
        Publication,
        PublicationAcknowledgement,
        PublicationContext,
        PublicationDelivery,
        RuntimeRecord,
    )
    from vs_slurm.api import ClusterSubmitOutcome, SlurmBatchRequest, SlurmJobRequest

GRACE_S = 60.0
"""The host's grace period (``STOP_GRACE_S`` in ``vibesys.run.host``): how long a stop may take
before the host cancels the loop."""
_HELD = 100_000
"""Observations a held job reports RUNNING for: longer than any run lasts."""


class TurnDiedError(RuntimeError):
    """An executor error: the process of an agent turn died."""


@dataclass(frozen=True)
class TurnSpan:
    """One agent turn on the virtual timeline."""

    worker: int
    start: float
    end: float
    cancelled: bool = False


@dataclass
class TurnTimeline:
    """How long agent turns last, and what they did on the virtual clock.

    A turn's worker is the order in which its session first dispatched (``0`` for the first
    session); ``duration(worker, ordinal)`` is how long that worker's ``ordinal``-th turn lasts.
    """

    clock: VirtualClock
    duration: Callable[[int, int], float]
    obeys_cancel: bool = True
    fails: Callable[[int, int], bool] = field(default=lambda _worker, _ordinal: False)
    """Whether that worker's ``ordinal``-th turn dies (its executor raises) when its time is up."""
    spans: list[TurnSpan] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    """The request ids of the turns that ended and handed their observation back."""
    starts: list[float] = field(default_factory=list)
    """When each turn began, in order (a turn is in ``spans`` only once it ended)."""
    _workers: dict[str, int] = field(default_factory=dict)
    _ordinals: dict[int, int] = field(default_factory=dict)
    _cancels: dict[str, asyncio.Event] = field(default_factory=dict)

    def worker_of(self, session: str) -> int:
        """The worker number of a session, assigned at its first turn."""
        return self._workers.setdefault(session, len(self._workers))

    def next_turn(self, worker: int) -> tuple[float, bool]:
        """How long this worker's next turn lasts, and whether it then dies."""
        ordinal = self._ordinals.get(worker, 0)
        self._ordinals[worker] = ordinal + 1
        return self.duration(worker, ordinal), self.fails(worker, ordinal)

    def cancel_event(self, invocation: str) -> asyncio.Event:
        """Set when the invocation's turn was asked to cancel."""
        return self._cancels.setdefault(invocation, asyncio.Event())

    def started_after(self, instant: float) -> list[TurnSpan]:
        """The turns that began after ``instant``."""
        return [span for span in self.spans if span.start > instant]

    def peak_overlap(self) -> int:
        """The most turns in flight at once."""
        edges = sorted(
            [(span.start, 1) for span in self.spans] + [(span.end, -1) for span in self.spans],
            key=lambda edge: (edge[0], edge[1]),
        )
        live = peak = 0
        for _, change in edges:
            live += change
            peak = max(peak, live)
        return peak


@dataclass(frozen=True)
class TimedTurns(RequestExecutors):
    """The real executors, with each agent turn lasting its duration on the virtual clock."""

    timeline: TurnTimeline | None = None

    @classmethod
    def around(cls, real: RequestExecutors, timeline: TurnTimeline) -> TimedTurns:
        """Wrap ``real``; every role is the real executor."""
        return cls(
            workspaces=real.workspaces,
            sessions=real.sessions,
            evaluation=real.evaluation,
            operations=real.operations,
            semantic_events=real.semantic_events,
            timeline=timeline,
        )

    async def dispatch(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        """Let a turn last its duration (or until cancelled), then run it for real."""
        assert self.timeline is not None
        timeline = self.timeline
        if isinstance(request, core.CancelTurn):
            timeline.cancel_event(request.invocation.invocation_id.root).set()
        if isinstance(request, core.DispatchTurn | core.ResumeSessionTurn):
            worker = timeline.worker_of(request.turn.session.session_id.root)
            start = timeline.clock.now()
            cancel = timeline.cancel_event(request.turn.invocation_id.root)
            timeline.starts.append(start)
            seconds, dies = timeline.next_turn(worker)
            cancelled = await self._last(seconds, cancel)
            timeline.spans.append(TurnSpan(worker, start, timeline.clock.now(), cancelled))
            if dies and not cancelled:
                message = f"the turn of worker {worker} died"
                raise TurnDiedError(message)
            outcome = await RequestExecutors.dispatch(self, request, context)
            assert request.request_id is not None
            timeline.returned.append(request.request_id.root)
            return outcome
        return await RequestExecutors.dispatch(self, request, context)

    async def _last(self, seconds: float, cancel: asyncio.Event) -> bool:
        """Sleep virtual time; True when a cancel ended the wait first (and the turn obeys it)."""
        assert self.timeline is not None
        if not self.timeline.obeys_cancel:
            await self.timeline.clock.sleep(seconds)
            return False
        sleeping = asyncio.ensure_future(self.timeline.clock.sleep(seconds))
        cancelled = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({sleeping, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeping, cancelled):
                task.cancel()
            await asyncio.gather(sleeping, cancelled, return_exceptions=True)
        return cancel.is_set()


class HoldingCluster(ScenarioCluster):
    """The Fake cluster whose jobs, from the ``hold_from``-th submission on, never end alone."""

    def __init__(self, clock: VirtualClock, hold_from: int | None = None) -> None:
        """Hold nothing unless ``hold_from`` (a submission ordinal, from 0) says so."""
        super().__init__()
        self.clock = clock
        self.hold_from = hold_from
        self.submitted_at: list[float] = []
        """The virtual time of each accepted submission, in order."""

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        """Script the job as held or completing by its ordinal, and note when it arrived."""
        before = len(self.submissions)
        held = self.hold_from is not None and before >= self.hold_from
        self.states = (SlurmJobStatus.RUNNING,) * _HELD if held else (SlurmJobStatus.COMPLETED,)
        outcome = super().submit(request, operation_id=operation_id)
        if len(self.submissions) > before:
            self.submitted_at.append(self.clock.now())
        return outcome


class StaggeredConfig(core.Value):
    """The staggered scenario: how many workers run their turn at once."""

    workers: int = 2


async def _worker(ctx: Flow, workspace: core.Workspace) -> str:
    """One turn in a session of its own, then the evaluation of what it made, at once."""
    session = ctx.agents.session(IMPLEMENTER_ROLE, workspace=workspace, revision=ctx.facts.baseline)
    match await session.turn(Prompt()):
        case TurnFinished(reply=Implementation(commit=commit)) if commit:
            plan = measurement(core.RevisionRef.of_git_commit(commit), "official")
            match await ctx.evaluation.measure(plan, workspace=workspace, within=880.0):
                case Failed():
                    return ""
            return commit
    return ""


@search(
    # The operations' kinds are namespaced by the search name (``OPERATIONS``).
    "skeleton",
    config=StaggeredConfig,
    operations=OPERATIONS,
    roles=(IMPLEMENTER_ROLE,),
)
async def staggered(ctx: Flow, config: StaggeredConfig) -> NoWinner | None:
    """Run ``config.workers`` turns at once, each followed by the evaluation of its change."""
    async with ctx.workspaces.open(
        attempt=core.AttemptId(root="attempt-0"),
        item=core.ItemId(root="item-0"),
        plan=core.WorkspacePlan(mode=core.WorkspaceMode.ISOLATED_CHILD, base=ctx.facts.baseline),
        budget=core.AttemptBudget(paid_invocation_limit=config.workers),
    ) as workspace:
        if isinstance(workspace, NotOpened):
            return NoWinner(reason="the attempt workspace did not open")
        async with ctx.group() as group:
            for number in range(config.workers):
                group.start(f"worker-{number}", _worker, workspace)
            async for _ in group:
                pass
    ctx.choose(INPUT)
    return None


class StaggeredStrategy(SkeletonStrategy):
    """The staggered scenario in the shape the skeleton world takes."""

    config: StaggeredConfig = StaggeredConfig()

    @property
    def declaration(self) -> core.SearchDeclaration:
        """What the host must offer this scenario."""
        return staggered.declaration

    def driver(self, registry: core.OperationRegistry | None = None) -> core.SearchDriver:
        """The driver that plays this scenario."""
        return staggered.driver(self.config, registry)


@dataclass
class TimedWorld(World):
    """A skeleton world whose agent turns last the durations of its timeline."""

    timeline: TurnTimeline | None = None

    def bindings(self) -> CoreRuntimeBindings:
        """The world's bindings, with the executors that let turns take virtual time."""
        bindings = super().bindings()
        assert self.timeline is not None
        return dataclasses.replace(
            bindings, executors=TimedTurns.around(bindings.executors, self.timeline)
        )


@contextmanager
def open_timed_world(
    tmp_path: Path,
    strategy: SkeletonStrategy,
    timeline: TurnTimeline,
    cluster: HoldingCluster | None = None,
) -> Iterator[TimedWorld]:
    """A skeleton world on in-memory Git whose turns follow ``timeline``."""
    with open_skeleton_world(tmp_path, strategy, cluster) as world:
        fields = {f.name: getattr(world, f.name) for f in dataclasses.fields(World)}
        yield TimedWorld(**fields, timeline=timeline)


@dataclass
class Observed:
    """What the run's commits showed: when the run first read as paused, and host liveness."""

    clock: VirtualClock
    paused_at: float | None = None
    alive: list[tuple[float, float]] = field(default_factory=list)
    """For each commit, its virtual time and the latest time core holds the host alive at."""

    def committed(self, previous: object, current: RuntimeRecord[Any]) -> None:
        """Note the first commit that leaves the run paused, and every commit's liveness."""
        del previous
        state = current.envelope.core
        paused = state.run.status == core.RunStatus.PAUSED
        if paused and self.paused_at is None:
            self.paused_at = self.clock.now()
        self.alive.append(
            (self.clock.now(), max(state.records.hosts[-1].alive_at, state.run.now_at))
        )


@dataclass(frozen=True)
class Timed:
    """A finished timed run and the records it left on the virtual timeline."""

    outcome: RunOutcome | None
    """How the loop returned; None when the host's grace bound cancelled it first."""
    gave_up: bool
    """Whether the host cancelled the loop at its grace bound."""
    started_at: float
    ended_at: float
    stopped_at: float | None
    pause_requested_at: float | None
    paused_at: float | None
    timeline: TurnTimeline
    cluster: HoldingCluster
    cancelled_during: tuple[str, ...]
    """The jobs the run itself cancelled before the loop returned."""
    cancelled_by_exit: tuple[str, ...]
    """The jobs the host's exit cancelled: it closes the evaluation executors."""
    holds_lease: bool
    """Whether the loop's host still held the run's lease when the loop returned."""
    core: CoreState
    """The run's state when the loop returned."""
    failure: Exception | None = None
    """What the loop raised (halting the run); None when it returned or was cancelled."""
    alive: tuple[tuple[float, float], ...] = ()
    """For each commit, its virtual time and the latest time core held the host alive at."""

    @property
    def wall_s(self) -> float:
        """Virtual seconds from the loop start to its end."""
        return self.ended_at - self.started_at


@dataclass(frozen=True)
class Plan:
    """What one timed run does; the fields a scenario leaves out are the quiet defaults."""

    duration: Callable[[int, int], float] = field(default=lambda _worker, _ordinal: 1.0)
    obeys_cancel: bool = True
    fails: Callable[[int, int], bool] = field(default=lambda _worker, _ordinal: False)
    hold_from: int | None = None
    max_parallel: int = 1
    max_turns: int = 8
    stop_when: Callable[[TurnTimeline, HoldingCluster], bool] | None = None
    mode: core.StopMode = core.StopMode.DRAIN
    pause_when: Callable[[TurnTimeline, HoldingCluster], bool] | None = None
    control_poll_s: float = 30.0
    observe_s: float = 1.0
    publish_s: float = 0.0
    """How long each publication takes to deliver, on the virtual clock."""


@dataclass
class _SlowDelivery:
    """Delivers each publication through ``inner`` after ``seconds`` of virtual time."""

    inner: PublicationDelivery
    clock: VirtualClock
    seconds: float

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        await self.clock.sleep(self.seconds)
        return await self.inner.publish(publication, context)


def run_timed(strategy: SkeletonStrategy, plan: Plan) -> Timed:
    """Play ``strategy`` on a fresh world, on virtual time, as ``plan`` says."""
    with tempfile.TemporaryDirectory() as directory:
        clock = VirtualClock(1.0)
        return run_virtual(clock, _play(Path(directory), clock, strategy, plan))


@dataclass
class _Operator:
    """The operator of a timed run: pauses when asked to, resumes once paused, stops when asked to."""

    plan: Plan
    clock: VirtualClock
    channel: RuntimeRunControlChannel
    timeline: TurnTimeline
    cluster: HoldingCluster
    observed: Observed
    marks: dict[str, float] = field(default_factory=dict)
    """The virtual time of each control sent: ``pause``, ``resume`` and ``stop``."""

    def _act(self) -> bool:
        """Send the controls the plan's conditions call for; whether the run was stopped."""
        plan, marks = self.plan, self.marks
        if (
            plan.pause_when is not None
            and "pause" not in marks
            and plan.pause_when(self.timeline, self.cluster)
        ):
            marks["pause"] = self.clock.now()
            self.channel.request_pause()
        if self.observed.paused_at is not None and "resume" not in marks:
            marks["resume"] = self.clock.now()
            self.channel.resume()
        if plan.stop_when is not None and plan.stop_when(self.timeline, self.cluster):
            marks["stop"] = self.clock.now()
            self.channel.request_stop(plan.mode)
            return True
        return False

    async def run(self) -> None:
        """Watch the run on the virtual clock until every control the plan asks for was sent."""
        plan = self.plan
        while not self._act():
            if plan.stop_when is None and (plan.pause_when is None or "resume" in self.marks):
                return
            await self.clock.sleep(0.25)


async def _play(path: Path, clock: VirtualClock, strategy: SkeletonStrategy, plan: Plan) -> Timed:
    timeline = TurnTimeline(clock, plan.duration, plan.obeys_cancel, plan.fails)
    cluster = HoldingCluster(clock, plan.hold_from)
    observed = Observed(clock)
    with open_timed_world(path, strategy, timeline, cluster) as world:
        world.commits = observed
        world.limits = sample_limits(
            max_turns=plan.max_turns,
            max_parallel=plan.max_parallel,
            observe_interval=plan.observe_s,
            observe_backoff_cap=plan.observe_s,
        )
        channel = RuntimeRunControlChannel(_ignore, threads=loop_threads())
        process = world.runtime()
        delivery = _SlowDelivery(process.delivery, clock, plan.publish_s)
        host = CoreRunHost(process.shell, delivery, clock, RunControlBridge(channel, _Steers()))
        config = RunLoopConfig(
            host_id="timed",
            lease_duration=LEASE,
            control_poll_interval=plan.control_poll_s,
            max_dispatches=5000,
        )
        start_core(host, config)
        started = clock.now()
        operator = _Operator(plan, clock, channel, timeline, cluster, observed)
        running = asyncio.ensure_future(drive_core(host, config))
        watching = asyncio.ensure_future(operator.run())
        outcome: RunOutcome | None = None
        failure: BaseException | None = None
        gave_up = False
        try:
            await asyncio.wait({running, watching}, return_when=asyncio.FIRST_COMPLETED)
            if "stop" in operator.marks and not running.done():
                # The host's bound on a stop: cancel the loop when the grace period passes.
                await asyncio.wait({running}, timeout=GRACE_S)
                if not running.done():
                    running.cancel()
                    gave_up = True
            await asyncio.gather(running, return_exceptions=True)
            if not gave_up and running.exception() is None:
                outcome = running.result()
            elif not gave_up:
                failure = running.exception()
        finally:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
        ended = clock.now()
        holds = process.shell.holds_lease(now_at=ended)
        before = tuple(cluster.cancelled)
        for evaluation in world.evaluations:
            await evaluation.close()
        return Timed(
            outcome,
            gave_up,
            started,
            ended,
            operator.marks.get("stop"),
            operator.marks.get("pause"),
            observed.paused_at,
            timeline,
            cluster,
            before,
            tuple(job for job in cluster.cancelled if job not in before),
            holds,
            process.shell.record.envelope.core,
            failure if isinstance(failure, Exception) else None,
            tuple(observed.alive),
        )


class _Steers:
    def put(self, ref: object, text: str) -> None:
        del ref, text


def _ignore(transition: object) -> None:
    del transition


__all__ = [
    "GRACE_S",
    "HoldingCluster",
    "Plan",
    "StaggeredConfig",
    "StaggeredStrategy",
    "Timed",
    "TimedTurns",
    "TimedWorld",
    "TurnSpan",
    "TurnTimeline",
    "open_timed_world",
    "run_timed",
]
