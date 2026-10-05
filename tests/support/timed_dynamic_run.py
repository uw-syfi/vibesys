"""The dynamic strategy on the production shell and loop, with durations on a virtual clock.

``tests/vibesys/orchestration/dynamic/strategy/_shell.py`` answers every request at once, so
a lease that lapses, a stop that waits for a turn, or two turns that never overlap cannot
show. Here the same production pieces (``CoreRuntime`` driven by ``drive_core``, the
lease-enforcing state store, the run-control bridge) run on one :class:`VirtualClock` and
the far side takes time like the real one:

- an agent turn lasts a duration drawn from ``TimingProfile.turn_s`` (a real run: 15 to
  45 s) and makes one mid-turn tool call that renews the lease with the turn's own
  request time, as the evaluation tool bridge does;
- an evaluation job is a job on the Fake Slurm cluster (``vs_slurm.FakeCluster``) over the
  same clock, so its queue wait, run and COMPLETING time come from the profile's
  ``SlurmTimingProfile`` (the one timing source for Slurm; nothing here redefines it).

Nothing waits on the wall clock: a run of minutes of virtual time takes milliseconds.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.virtual_time import VirtualClock, run_virtual
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, LIMITS, config
from tests.vibesys.orchestration.dynamic.strategy._shell import ScriptedExecutors, _executors

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vibesys.run.core_run import LEASE_SECONDS
from vs_core.api import (
    DispatchTurn,
    JobObserved,
    ObservationStatus,
    ObserveOwnedJob,
    ResourceId,
    ResumeSessionTurn,
    RunEnvelope,
    RunResultProposal,
    SubmitMeasurement,
)
from vs_core.testing.drive import Answer, Harness, Running, Succeeded, new_run
from vs_project.api import FakeStateStore, StoreFence
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    RunControlBridge,
    RunLoopConfig,
    RunOutcome,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RuntimeRunControlChannel
from vs_runtime.api.testing import FakePublicationDelivery
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    SecondsRange,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmTimingProfile,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, OperationRegistry, Request, SchemaRef
    from vs_runtime.api.core import ExecutionContext, ExecutionResult

IMPLEMENTERS = 2
# A mid-turn tool call happens this far into a turn.
_TOOL_CALL_AT = 0.5
# Core polls a running job this often (the production default is 10 s). Each poll and each
# loop wake commits a clock tick, about 20 ms of host time for the immutability checks, so
# a coarser cadence keeps a run of minutes at about a second of host time.
OBSERVE_INTERVAL_S = 30.0
_CONTROL_POLL_S = OBSERVE_INTERVAL_S


@dataclass(frozen=True)
class TimingProfile:
    """How long the far side takes. Defaults are the measured real run."""

    turn_s: SecondsRange = field(default_factory=lambda: SecondsRange(low=15.0, high=45.0))
    slurm: SlurmTimingProfile = field(default_factory=SlurmTimingProfile)
    seed: int = 0

    def turn_duration(self, key: str) -> float:
        """The deterministic duration of the turn named ``key``."""
        digest = hashlib.sha256(f"{self.seed}/{key}".encode()).digest()
        fraction = int.from_bytes(digest[:6], "big") / float(1 << 48)
        return self.turn_s.low + fraction * (self.turn_s.high - self.turn_s.low)


@dataclass(frozen=True)
class TurnSpan:
    """One agent turn on the virtual timeline."""

    role: str
    start: float
    end: float
    cancelled: bool = False


@dataclass(frozen=True)
class LeaseEvent:
    """One acquisition or renewal of the state-store lease."""

    kind: str
    at: float
    accepted: bool


class LeaseRecordingStore(FakeStateStore):
    """The Fake store, noting each lease acquisition and renewal with the time it carried."""

    def __init__(self) -> None:
        """Start with no lease events."""
        super().__init__()
        self.lease_events: list[LeaseEvent] = []

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        """Acquire as the Fake does and record it."""
        fence = super().acquire(host_id, now, duration)
        self.lease_events.append(LeaseEvent("acquire", now, fence is not None))
        return fence

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        """Renew as the Fake does and record it."""
        renewed = super().renew(fence, now, duration)
        self.lease_events.append(LeaseEvent("renew", now, renewed is not None))
        return renewed


class TimedCluster:
    """Evaluation jobs on the Fake Slurm cluster, finished when its clock says so."""

    def __init__(self, cluster: FakeCluster, workspace: Path) -> None:
        """Bind the Fake cluster and the directory its jobs are submitted from."""
        self._cluster = cluster
        self._workspace = workspace
        self.jobs: list[tuple[str, float]] = []

    def submit(self, request: SubmitMeasurement) -> Answer:
        """Accept the job under an identity derived from the request; it starts pending."""
        assert request.request_id is not None
        operation = "job-" + hashlib.sha256(request.request_id.root.encode()).hexdigest()[:16]
        outcome = self._cluster.submit(
            SlurmJobRequest(workspace=self._workspace, command=("true",)), operation_id=operation
        )
        assert isinstance(outcome, ClusterSubmitted), outcome
        self.jobs.append((operation, self._cluster.clock.now()))
        return Running(resource_id=ResourceId(root=f"job:{operation}"))

    def finished(self, resource_id: ResourceId) -> bool:
        """Whether the scheduler reports the job in a terminal state."""
        observed = self._cluster.inspect(resource_id.root.removeprefix("job:"))
        assert isinstance(observed, ClusterObservation), observed
        return observed.status in {
            SlurmJobStatus.COMPLETED,
            SlurmJobStatus.FAILED,
            SlurmJobStatus.CANCELLED,
        }


class _TimedScript:
    """The scripted answers, except that a job is running until the cluster finishes it."""

    def __init__(self, inner: Executors, cluster: TimedCluster) -> None:
        self._inner = inner
        self._cluster = cluster

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, ObserveOwnedJob) and not self._cluster.finished(request.resource_id):
            # The poll itself succeeded; the job it looked at is still going (see below).
            return Succeeded(resource_id=request.resource_id)
        return self._inner(request, core)


@dataclass(frozen=True)
class _World:
    """The timeline the executors act on and the records they leave on it."""

    clock: VirtualClock
    profile: TimingProfile
    turns: list[TurnSpan]
    cluster: TimedCluster


class _TimedExecutors(ScriptedExecutors):
    """Agent turns take drawn virtual time; every other request is answered at once."""

    def __init__(
        self,
        script: _TimedScript,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        world: _World,
    ) -> None:
        super().__init__(script, core, registry, schemas)
        self._cluster = world.cluster
        self._clock = world.clock
        self._profile = world.profile
        self._turns = world.turns

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        if isinstance(request, ObserveOwnedJob):
            return self._polled(request, await super().execute(request, context))
        if not isinstance(request, DispatchTurn | ResumeSessionTurn):
            return await super().execute(request, context)
        assert request.request_id is not None
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        duration = self._profile.turn_duration(request.request_id.root)
        start = self._clock.now()
        try:
            await self._clock.sleep(duration * _TOOL_CALL_AT)
            # The tool bridge renews with the time of the request the call belongs to.
            if context.lease is not None:
                context.lease.renew(now_at=context.now_at, lease_duration=LEASE_SECONDS)
            await self._clock.sleep(duration * (1 - _TOOL_CALL_AT))
        except BaseException:
            self._turns.append(TurnSpan(role, start, self._clock.now(), cancelled=True))
            raise
        self._turns.append(TurnSpan(role, start, self._clock.now()))
        return await super().execute(request, context)

    def _polled(self, request: ObserveOwnedJob, result: ExecutionResult) -> ExecutionResult:
        """A poll is a terminal request; the job it saw is non-terminal until the cluster ends it.

        As in production, the request's own observation succeeds and the job's state rides
        the owner event, so a job the cluster has not finished is reported pending.
        """
        if self._cluster.finished(request.resource_id):
            return result
        events = tuple(
            event.model_copy(
                update={
                    "observation": event.observation.model_copy(
                        update={
                            "status": ObservationStatus.PENDING,
                            "terminal": False,
                            "released": False,
                            "children_complete": False,
                        }
                    )
                }
            )
            if isinstance(event, JobObserved)
            else event
            for event in result.owner_events
        )
        return result.model_copy(update={"owner_events": events})


class _Steers:
    def put(self, ref: object, text: str) -> None:
        del ref, text


def _ignore(transition: object) -> None:
    del transition


@dataclass
class TimedRun:
    """What one run did on the virtual timeline."""

    core: CoreState
    started_at: float
    ended_at: float
    outcome: RunOutcome | None
    error: BaseException | None
    turns: list[TurnSpan]
    lease_events: list[LeaseEvent]
    jobs: list[tuple[str, float]]
    stopped_at: float | None = None

    @property
    def wall_s(self) -> float:
        """Virtual seconds from the loop start to its end."""
        return self.ended_at - self.started_at

    def max_overlap(self, role: str) -> int:
        """The most turns of ``role`` in flight at once."""
        edges = sorted(
            [(span.start, 1) for span in self.turns if span.role == role]
            + [(span.end, -1) for span in self.turns if span.role == role],
            key=lambda edge: (edge[0], edge[1]),
        )
        live = peak = 0
        for _, change in edges:
            live += change
            peak = max(peak, live)
        return peak


def run_timed(profile: TimingProfile, *, stop_after: float | None = None) -> TimedRun:
    """Run one round of two workstreams on the production shell and loop, on virtual time.

    ``stop_after`` asks the run to stop that many virtual seconds after the loop starts
    (the operator's stop, through the run-control channel).
    """
    with tempfile.TemporaryDirectory() as directory:
        return _run(profile, Path(directory), stop_after)


def _run(profile: TimingProfile, workspace: Path, stop_after: float | None) -> TimedRun:
    clock = VirtualClock(1.0)
    cluster = TimedCluster(
        FakeCluster(clock=clock, timing=profile.slurm, seed=profile.seed), workspace
    )
    script = Executors(
        planner=deque([plan_reply(*(implement(f"h{n}") for n in range(IMPLEMENTERS)))]),
        implementer=deque(implemented() for _ in range(IMPLEMENTERS)),
        judge=deque(reviewed() for _ in range(IMPLEMENTERS)),
        submit=cluster.submit,
    )
    selected = config(max_in_flight=IMPLEMENTERS)
    harness = Harness(
        registry=dynamic_operation_registry(),
        facts=FACTS,
        limits=LIMITS.model_copy(update={"observe_interval": OBSERVE_INTERVAL_S}),
        envelope_type=RunEnvelope[DynamicStrategyState],
        requirements=requirements_for(selected),
    )
    strategy = DynamicStrategy(config=selected)
    store = LeaseRecordingStore()
    turns: list[TurnSpan] = []
    shell: CoreRuntime[DynamicStrategyState] = CoreRuntime(
        store,
        strategy,
        new_run(strategy, harness),
        bindings=CoreRuntimeBindings(
            registry=harness.registry,
            executors=_executors(
                _TimedExecutors(
                    _TimedScript(script, cluster),
                    lambda: shell.record.envelope.core,
                    harness.registry,
                    reply_schemas(selected),
                    _World(clock, profile, turns, cluster),
                )
            ),
        ),
    )
    channel = RuntimeRunControlChannel(_ignore)
    controls = RunControlBridge(
        channel, _Steers(), stop_result=RunResultProposal(outcome="cancelled", reason="operator")
    )
    host = CoreRunHost(shell, FakePublicationDelivery(store), clock, controls)
    # Production polls controls every second of run time. A stop does not wait for the
    # poll (the loop watches the channel), so a coarser poll does not change what the
    # properties observe.
    loop_config = RunLoopConfig(
        host_id="timed",
        lease_duration=LEASE_SECONDS,
        control_poll_interval=_CONTROL_POLL_S,
        max_dispatches=400,
    )
    start_core(host, loop_config)
    started = clock.now()
    stopped: list[float] = []

    async def stop_later(seconds: float) -> None:
        await clock.sleep(seconds)
        stopped.append(clock.now())
        channel.request_stop()

    async def main() -> tuple[RunOutcome | None, BaseException | None]:
        timer = asyncio.ensure_future(stop_later(stop_after)) if stop_after is not None else None
        try:
            return await drive_core(host, loop_config), None
        # lint-waiver: LW-990101 [BLE001]; the run's own failure is the observation: a test
        # > asserts its type (a lapsed lease, a stop) and the virtual time it happened at.
        except BaseException as error:  # noqa: BLE001
            return None, error
        finally:
            if timer is not None:
                timer.cancel()

    outcome, error = run_virtual(clock, main())
    return TimedRun(
        core=shell.record.envelope.core,
        started_at=started,
        ended_at=clock.now(),
        outcome=outcome,
        error=error,
        turns=turns,
        lease_events=store.lease_events,
        jobs=cluster.jobs,
        stopped_at=stopped[0] if stopped else None,
    )
