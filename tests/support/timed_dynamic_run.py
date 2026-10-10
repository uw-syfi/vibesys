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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tests.support.liveness import Journal
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors, lease_for
from tests.vibesys.orchestration.dynamic.strategy._shell import ScriptedExecutors

from vs_core.api import (
    CancelTurn,
    DispatchTurn,
    InvocationRef,
    JobObserved,
    ObservationStatus,
    ObserveOwnedJob,
    ResourceId,
    ResumeSessionTurn,
    RunStatus,
    SubmitMeasurement,
    TurnObserved,
)
from vs_core.testing.drive import Answer, Running, Succeeded
from vs_project.api import FakeStateStore, StoreFence
from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS as LEASE_SECONDS
from vs_runtime.api.core import (
    ExecutionResult,
)
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
    from pathlib import Path

    from pydantic import BaseModel
    from tests.support.virtual_time import VirtualClock

    from vs_core.api import CoreState, OperationRegistry, Request, SchemaRef
    from vs_runtime.api.core import ExecutionContext, RuntimeRecord

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


class TimedScript:
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
class World:
    """The timeline the executors act on and the records they leave on it."""

    clock: VirtualClock
    profile: TimingProfile
    turns: list[TurnSpan]
    cluster: TimedCluster
    journal: Journal = field(default_factory=Journal)


class TimedExecutors(ScriptedExecutors):
    """Agent turns take drawn virtual time; every other request is answered at once."""

    def __init__(
        self,
        script: TimedScript,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        world: World,
    ) -> None:
        super().__init__(script, core, registry, schemas, world.journal)
        self._cluster = world.cluster
        self._clock = world.clock
        self._profile = world.profile
        self._turns = world.turns
        self._cancels: dict[str, asyncio.Event] = {}

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        if isinstance(request, CancelTurn):
            self._cancels.setdefault(request.invocation.invocation_id.root, asyncio.Event()).set()
            return self._cancelled(request, context)
        if isinstance(request, ObserveOwnedJob):
            return self._polled(request, await super().execute(request, context))
        if not isinstance(request, DispatchTurn | ResumeSessionTurn):
            return await super().execute(request, context)
        assert request.request_id is not None
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        duration = self._profile.turn_duration(request.request_id.root)
        start = self._clock.now()
        cancel = self._cancels.setdefault(request.turn.invocation_id.root, asyncio.Event())
        try:
            interrupted = await self._sleep_unless(duration * _TOOL_CALL_AT, cancel)
            if not interrupted:
                # The tool bridge renews with the time of the request the call belongs to.
                if context.lease is not None:
                    context.lease.renew(now_at=context.now_at, lease_duration=LEASE_SECONDS)
                interrupted = await self._sleep_unless(duration * (1 - _TOOL_CALL_AT), cancel)
        except BaseException:
            self._turns.append(TurnSpan(role, start, self._clock.now(), cancelled=True))
            raise
        if interrupted:
            # The provider turn was interrupted: accepted, ended, no reply.
            self._turns.append(TurnSpan(role, start, self._clock.now(), cancelled=True))
            return self._cancelled(request, context)
        self._turns.append(TurnSpan(role, start, self._clock.now()))
        return await super().execute(request, context)

    def _cancelled(
        self, request: CancelTurn | DispatchTurn | ResumeSessionTurn, context: ExecutionContext
    ) -> ExecutionResult:
        """What the executor reports for a request that ended cancelled: accepted, released."""
        session = (
            request.invocation.session_id
            if isinstance(request, CancelTurn)
            else request.turn.session.session_id
        )
        observed = self._observed(
            request, Succeeded(resource_id=lease_for(session)), context.now_at
        )
        view = observed.observation.model_copy(update={"status": ObservationStatus.CANCELLED})
        events = (
            (
                TurnObserved(
                    invocation=InvocationRef(
                        session_id=request.turn.session.session_id,
                        invocation_id=request.turn.invocation_id,
                        generation=request.scope.generation,
                    ),
                    observation=view,
                ),
            )
            if isinstance(request, DispatchTurn | ResumeSessionTurn)
            else ()
        )
        return ExecutionResult(
            observation=observed.model_copy(update={"observation": view}), owner_events=events
        )

    async def _sleep_unless(self, seconds: float, cancel: asyncio.Event) -> bool:
        """Sleep virtual time; returns True when a CancelTurn ended the turn first."""
        sleeping = asyncio.ensure_future(self._clock.sleep(seconds))
        cancelled = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({sleeping, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeping, cancelled):
                task.cancel()
            await asyncio.gather(sleeping, cancelled, return_exceptions=True)
        return cancel.is_set()

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


class CommitCapExceededError(RuntimeError):
    """The run committed more often than the scenario allows: it is spinning, not settling."""


class PauseClock:
    """Notes the virtual time of the first commit that leaves the run paused.

    With ``max_commits`` it also fails a run that keeps committing without settling: a loop
    that spins on a rejected decision commits forever and never reaches a dispatch cap.
    """

    def __init__(
        self, clock: VirtualClock, paused: list[float], max_commits: int | None = None
    ) -> None:
        self._clock = clock
        self._paused = paused
        self._left = max_commits

    def committed(self, previous: object, current: RuntimeRecord[Any]) -> None:
        del previous
        if self._left is not None:
            self._left -= 1
            if self._left < 0:
                raise CommitCapExceededError
        if current.envelope.core.run.status == RunStatus.PAUSED and not self._paused:
            self._paused.append(self._clock.now())


class Steers:
    def put(self, ref: object, text: str) -> None:
        del ref, text


def ignore_publication(transition: object) -> None:
    del transition
