"""The dynamic strategy on the production shell and loop, on virtual time, at any scale.

This is the one runner for virtual-time runs of the dynamic strategy. It runs the production
pieces (``CoreRuntime`` under ``drive_core``, the lease-enforcing store, the run-control
bridge, the Fake Slurm cluster over one :class:`VirtualClock`) with any number of
workstreams in flight and any number of rounds, so contention, bursts of simultaneous
completions and long campaigns show. ``run_timed`` is the small preset the timing tests use
(one round of two workstreams), not a second runner. The building blocks (timing profile,
timed executors, cluster) live in ``tests/support/timed_dynamic_run.py``.

The far side is scripted: the planner fills exactly the slots its call was opened for (read
from the strategy state, as an agent reads its prompt) with fresh hypothesis ids, and
implementer and judge replies come from callables keyed by the turn's sequence, so a
scenario can make some workstreams fail, some reviews reject and some benchmarks fail.

Controls are virtual-time offsets from the start of the loop: `stop_after`,
`pause_after`/`resume_after`, and `deadline_at` (the run's own deadline).

Every run is checked for liveness (``tests/support/liveness.py``) unless the scenario opts
out: a run that ends terminal or stopped must also have bounded requests, no spin, no open
intent and no orphan wait. Reaching ``max_dispatches`` is a failure, never a quiet stop, and so is committing more than
``max_commits`` times (a loop spinning on a refused decision).

Cost: every commit validates the whole envelope, so a run grows roughly quadratically with
its size. Keep PR-tier scenarios to a few workstreams and rounds.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.liveness import Budget, End, Journal, assert_live
from tests.support.timed_dynamic_run import (
    IMPLEMENTERS,
    OBSERVE_INTERVAL_S,
    LeaseEvent,
    LeaseRecordingStore,
    PauseClock,
    Steers,
    TimedCluster,
    TimedExecutors,
    TimedScript,
    TimingProfile,
    TurnSpan,
    World,
    ignore_publication,
)
from tests.support.virtual_time import VirtualClock, run_virtual
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors, lease_for
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, LIMITS, config
from tests.vibesys.orchestration.dynamic.strategy._shell import (
    RecordingStrategy,
    request_executors,
)

from vibesys.orchestration.dynamic.core_policy.api import (
    limits_for,
    reply_schemas,
    requirements_for,
)
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import DispatchTurn, Limits, RunEnvelope, RunResultProposal, SubmitMeasurement
from vs_core.testing.drive import Harness, Succeeded, Unknown, new_run
from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS as LEASE_SECONDS
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    DispatchCapExceededError,
    RunControlBridge,
    RunLoopConfig,
    RunOutcome,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RunStopped, RuntimeRunControlChannel
from vs_runtime.api.testing import FakePublicationDelivery
from vs_slurm.api import FakeCluster

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState, Decision
    from vs_core.testing.drive import Answer


def _always_implemented(_n: int) -> str | Answer:
    return implemented()


def _always_passed(_n: int) -> str | Answer:
    return reviewed()


@dataclass(frozen=True)
class Scenario:
    """One scale scenario: the shape of the run and how the far side answers."""

    in_flight: int = 4
    rounds: int = 3
    profile: TimingProfile = field(default_factory=TimingProfile)
    limits: Limits | None = None
    implementer: Callable[[int], str | Answer] = _always_implemented
    judge: Callable[[int], str | Answer] = _always_passed
    benchmark: Callable[[str], float | None] = lambda _commit: 100.0
    stop_after: float | None = None
    pause_after: float | None = None
    resume_after: float | None = None
    max_dispatches: int = 5000
    max_commits: int = 6000
    max_concurrent: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    deadline_at: float = 100000.0
    live: bool = True

    def run_limits(self, selected: DynamicConfig) -> Limits:
        """The given limits, else the ones production derives from the strategy config."""
        if self.limits is not None:
            return self.limits
        return limits_for(
            selected,
            observe_interval=OBSERVE_INTERVAL_S,
            observe_backoff_cap=max(120.0, OBSERVE_INTERVAL_S),
        )


class _ScaleScript(Executors):
    """Scripted answers whose planner fills its call's slots and whose agents never run dry."""

    def __init__(
        self,
        scenario: Scenario,
        state: Callable[[], DynamicStrategyState],
        submit: Callable[[SubmitMeasurement], Answer],
    ) -> None:
        super().__init__(benchmark=scenario.benchmark, submit=submit)
        self._scenario = scenario
        self._state = state
        self.planned = 0
        self.turns: Counter[str] = Counter()

    def _turn(self, request: DispatchTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        index = self.turns[role]
        self.turns[role] += 1
        # test-isolation: the scripted executors name a session's lease this way, from ensure to close
        lease = lease_for(request.turn.session.session_id)
        if role == "orchestrator":
            want = self._state().planner.capacity
            entries = []
            for _ in range(want):
                entries.append(implement(f"h{self.planned}"))
                self.planned += 1
            return Succeeded(output_json=plan_reply(*entries), resource_id=lease)
        reply = (self._scenario.implementer if role == "implementer" else self._scenario.judge)(
            index
        )
        if reply is None:
            return Unknown()
        if not isinstance(reply, str):
            return reply
        return Succeeded(output_json=reply, resource_id=lease)


@dataclass
class ScaleRun:
    """What one scale run did on the virtual timeline."""

    core: CoreState
    strategy: DynamicStrategyState
    started_at: float
    ended_at: float
    outcome: RunOutcome | None
    error: BaseException | None
    turns: list[TurnSpan]
    lease_events: list[LeaseEvent]
    jobs: list[tuple[str, float]]
    journal: Journal
    agent_turns: Counter[str]
    cluster: FakeCluster
    decisions: list[Decision]
    stopped_at: float | None = None
    pause_requested_at: float | None = None
    paused_at: float | None = None
    """Virtual time of the first commit that left the run paused."""

    @property
    def wall_s(self) -> float:
        """Virtual seconds from the loop start to its end."""
        return self.ended_at - self.started_at

    def max_overlap(self, role: str | None = None) -> int:
        """The most turns (of ``role``, or of any role) in flight at once."""
        spans = [s for s in self.turns if role is None or s.role == role]
        edges = sorted([(s.start, 1) for s in spans] + [(s.end, -1) for s in spans])
        live = peak = 0
        for _, change in edges:
            live += change
            peak = max(peak, live)
        return peak


def run_timed(
    profile: TimingProfile, *, stop_after: float | None = None, pause_after: float | None = None
) -> ScaleRun:
    """One round of two workstreams, the preset the timing tests use."""
    return run_scale(
        Scenario(
            in_flight=IMPLEMENTERS,
            rounds=1,
            profile=profile,
            limits=LIMITS.model_copy(update={"observe_interval": OBSERVE_INTERVAL_S}),
            stop_after=stop_after,
            pause_after=pause_after,
            max_dispatches=400,
        )
    )


def run_scale(scenario: Scenario) -> ScaleRun:
    """Run ``scenario`` on the production shell and loop, on virtual time.

    Raises ``DispatchCapExceededError`` when the run hits ``max_dispatches``, and the
    liveness violations of a run that ended terminal or stopped (unless ``live`` is off).
    """
    with tempfile.TemporaryDirectory() as directory:
        return _Simulation(scenario, Path(directory)).play()


class _Simulation:
    """One scenario's world: the clock, the far side, and the host processes over one store."""

    def __init__(self, scenario: Scenario, workspace: Path) -> None:
        self.scenario = scenario
        profile = scenario.profile
        self.clock = VirtualClock(1.0)
        self.fake = FakeCluster(clock=self.clock, timing=profile.slurm, seed=profile.seed)
        self.cluster = TimedCluster(self.fake, workspace)
        self.selected = config(
            max_in_flight=scenario.in_flight, max_rounds=scenario.rounds, **scenario.config
        )
        self.limits = scenario.run_limits(self.selected)
        self.harness = Harness(
            registry=dynamic_operation_registry(),
            facts=FACTS,
            limits=self.limits,
            envelope_type=RunEnvelope[DynamicStrategyState],
            requirements=requirements_for(self.selected),
            deadline_at=scenario.deadline_at,
        )
        self.strategy = DynamicStrategy(config=self.selected)
        self.store = LeaseRecordingStore()
        self.paused: list[float] = []
        self.turns: list[TurnSpan] = []
        self.decisions: list[Decision] = []
        self.shells: list[CoreRuntime[DynamicStrategyState]] = []
        self.script = _ScaleScript(
            scenario, lambda: self.shells[-1].record.envelope.strategy, submit=self.cluster.submit
        )
        self.world = World(self.clock, profile, self.turns, self.cluster)
        self.channel = RuntimeRunControlChannel(ignore_publication)
        self.controls = RunControlBridge(
            self.channel,
            Steers(),
            stop_result=RunResultProposal(outcome="cancelled", reason="operator"),
        )
        self.loop_config = RunLoopConfig(
            host_id="scale",
            lease_duration=LEASE_SECONDS,
            control_poll_interval=OBSERVE_INTERVAL_S,
            max_dispatches=scenario.max_dispatches,
            max_concurrent=scenario.max_concurrent,
        )
        self.stopped: list[float] = []
        self.pause_requested: list[float] = []

    def host_on_store(self) -> CoreRunHost:
        """The host process: a shell over the durable store."""
        shell: CoreRuntime[DynamicStrategyState] = CoreRuntime(
            self.store,
            RecordingStrategy(self.strategy, self.decisions),
            new_run(self.strategy, self.harness),
            bindings=CoreRuntimeBindings(
                registry=self.harness.registry,
                commits=PauseClock(self.clock, self.paused, self.scenario.max_commits),
                executors=request_executors(
                    TimedExecutors(
                        TimedScript(self.script, self.cluster),
                        lambda: shell.record.envelope.core,
                        self.harness.registry,
                        reply_schemas(self.selected),
                        self.world,
                    )
                ),
            ),
        )
        self.shells.append(shell)
        return CoreRunHost(shell, FakePublicationDelivery(self.store), self.clock, self.controls)

    def play(self) -> ScaleRun:
        """Start the first host, run the scenario to its end, and check it."""
        host = self.host_on_store()
        start_core(host, self.loop_config)
        started = self.clock.now()
        outcome, error = run_virtual(self.clock, self._main(host))
        if isinstance(error, DispatchCapExceededError):
            raise error
        core = self.shells[-1].record.envelope.core
        if self.scenario.live and (error is None or isinstance(error, RunStopped)):
            assert_live(
                self.world.journal,
                core,
                Budget(retries=self.limits.max_retries),
                End.TERMINAL if error is None else End.STOPPED,
            )
        return self._result(core, started, outcome, error)

    async def _later(self, seconds: float, action: Callable[[], None], note: list[float]) -> None:
        await self.clock.sleep(seconds)
        note.append(self.clock.now())
        action()

    def _controls(self) -> list[asyncio.Future[None]]:
        scenario, channel = self.scenario, self.channel
        timers = []
        if scenario.stop_after is not None:
            timers.append(self._later(scenario.stop_after, channel.request_stop, self.stopped))
        if scenario.pause_after is not None:
            timers.append(
                self._later(scenario.pause_after, channel.request_pause, self.pause_requested)
            )
        if scenario.resume_after is not None:
            timers.append(self._later(scenario.resume_after, channel.resume, []))
        return [asyncio.ensure_future(timer) for timer in timers]

    async def _main(self, host: CoreRunHost) -> tuple[RunOutcome | None, BaseException | None]:
        tasks = self._controls()
        try:
            return await drive_core(host, self.loop_config), None
        # lint-waiver: LW-994698 [BLE001]; the run's own failure is the observation a test
        # > asserts on, as in `timed_dynamic_run`.
        except BaseException as error:  # noqa: BLE001
            return None, error
        finally:
            for task in tasks:
                task.cancel()

    def _result(
        self,
        core: CoreState,
        started: float,
        outcome: RunOutcome | None,
        error: BaseException | None,
    ) -> ScaleRun:
        return ScaleRun(
            core=core,
            strategy=self.shells[-1].record.envelope.strategy,
            started_at=started,
            ended_at=self.clock.now(),
            outcome=outcome,
            error=error,
            turns=self.turns,
            lease_events=self.store.lease_events,
            jobs=self.cluster.jobs,
            journal=self.world.journal,
            agent_turns=self.script.turns,
            cluster=self.fake,
            decisions=self.decisions,
            stopped_at=self.stopped[0] if self.stopped else None,
            pause_requested_at=self.pause_requested[0] if self.pause_requested else None,
            paused_at=self.paused[0] if self.paused else None,
        )
