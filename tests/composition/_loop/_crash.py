"""Crash and replay (conformance C2): kill the host at a durable boundary and restart it.

A "durable boundary" is one committed envelope. `CrashingStore` dies at the k-th commit,
either just before it reaches the store or just after (the three crash points of
`libs/vs-runtime/tests/test_core_dispatch.py`: before the commit of a step, after the commit
and before dispatch, and after dispatch and before the observation is committed; the third is
"before the commit" of an observation). The restarted host is a new `CoreRuntime` and a new
strategy instance over the store the dead one left, and the executors keep their world:
`RecoverableExecutors` seals what each request's effect did, so a restarted host that inspects
a request learns its result instead of running it again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from tests.composition._loop.conformance import Tape, Taped

from vs_core.api import (
    EventId,
    InspectRequest,
    Observation,
    ObservationStatus,
    ReissueProof,
    RequestObserved,
    RunStatus,
    TargetObservation,
)
from vs_core.testing.drive import Succeeded, new_run
from vs_project.api import CommitOutcome, FakeStateStore, StoredEnvelope, StoreFence
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    ExecutionResult,
    RunLoopConfig,
    drive_core,
    start_core,
)
from vs_runtime.api.testing import (
    LEASE,
    FakePublicationDelivery,
    Script,
    ScriptedExecutors,
    request_executors,
)
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel
    from tests.composition._loop.conformance import Subject, World

    from vs_core.api import (
        CoreState,
        Decision,
        OperationRegistry,
        Request,
        SchemaRef,
    )
    from vs_runtime.api.core import ExecutionContext, OwnerEvent


def as_target_fields(observed: RequestObserved) -> dict[str, object]:
    """Every fact of an executor's observation that a target observation carries."""
    skipped = {"kind", "target", "outcome"}
    return {
        name: getattr(observed, name)
        for name in RequestObserved.model_fields
        if name not in skipped
    }


class CrashPoint(StrEnum):
    """Where around the k-th commit the host dies."""

    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"


class HostDiedError(BaseException):
    """The process died. A `BaseException`, so nothing in the loop can absorb it."""


class CrashingStore(FakeStateStore):
    """A store that kills its host around one commit, counting every commit it is asked for."""

    def __init__(self, crash_at: int | None, point: CrashPoint) -> None:
        """Die at commit number ``crash_at`` (0-based) at ``point``; never when it is None."""
        super().__init__()
        self.commits = 0
        self._crash_at = crash_at
        self._point = point

    def commit(
        self,
        expected_revision: int | None,
        envelope: StoredEnvelope,
        fence: StoreFence,
        now: float,
    ) -> CommitOutcome:
        """Commit, dying before or after it if this is the chosen commit."""
        index = self.commits
        self.commits += 1
        chosen = self._crash_at == index
        if chosen and self._point is CrashPoint.BEFORE_COMMIT:
            raise HostDiedError
        outcome = super().commit(expected_revision, envelope, fence, now)
        if chosen:
            raise HostDiedError
        return outcome


@dataclass
class Effects:
    """What the executors' world did, kept across restarts of the host."""

    sealed: dict[str, ExecutionResult] = field(default_factory=dict)
    """The result each request's effect left behind: what a restarted host inspects."""
    executed: list[str] = field(default_factory=list)
    """Requests whose effect ran, in order; a request in it twice ran twice."""
    sequences: dict[str, int] = field(default_factory=dict)
    """Observation sequences issued per request, which a restart continues."""


class RecoverableExecutors(ScriptedExecutors):
    """Scripted executors that answer an inspection from what a request's effect sealed."""

    def __init__(
        self,
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        effects: Effects,
    ) -> None:
        """Bind the script and the shared record of effects."""
        super().__init__(script, core, registry, schemas)
        self._effects = effects
        self._registry = registry

    def _next(self, key: str) -> int:
        sequences = self._effects.sequences
        sequences[key] = sequences.get(key, -1) + 1
        return sequences[key]

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """Perform the request once and seal its result; answer an inspection from the seal."""
        if isinstance(request, InspectRequest):
            return self._inspect(request, context)
        assert request.request_id is not None
        self._effects.executed.append(request.request_id.root)
        result = await super().execute(request, context)
        self._effects.sealed[request.request_id.root] = result
        return result

    def _target(self, observed: RequestObserved) -> TargetObservation:
        """The target facts of a sealed observation; a typed outcome needs the operation codec."""
        facts = as_target_fields(observed)
        return TargetObservation.model_validate(
            facts, context={"operation_registry": self._registry}
        )

    def _inspect(self, request: InspectRequest, context: ExecutionContext) -> ExecutionResult:
        """What a restarted host learns of a request: its sealed result, or that it never ran."""
        target = request.target
        sealed = self._effects.sealed.get(target.root)
        owner_events: tuple[OwnerEvent, ...] = ()
        if sealed is not None:
            facts = self._target(sealed.observation)
            owner_events = sealed.owner_events
        else:
            sequence = self._next(target.root)
            observation = Observation(
                event_id=EventId(root=f"{target.root}:observation:{sequence}"),
                request_id=target,
                scope=request.scope,
                admission_id=request.admission_id,
                sequence=sequence,
                observed_at=context.now_at,
                status=ObservationStatus.REJECTED,
                accepted=False,
                terminal=True,
                released=False,
                children_complete=False,
            )
            facts = TargetObservation(observation=observation, reissue=ReissueProof.NEVER_BEGAN)
        own = self._observed(request, Succeeded(), context.now_at)
        return ExecutionResult(
            observation=own.model_copy(update={"target": facts}), owner_events=owner_events
        )


@dataclass
class Survived:
    """A run that was killed some number of times and finished."""

    core: CoreState
    state: object
    decisions: list[Decision]
    effects: Effects
    commits: int
    crashed: int


def run_with_crash(
    subject: Subject, world: World, crash_at: int | None, point: CrashPoint
) -> Survived:
    """Run ``subject`` to the end, killing the host once at the ``crash_at``-th commit.

    Each host is a new `CoreRuntime` and a new strategy instance over the one store; the
    world and the effects ledger are the same objects throughout, as the outside world is.
    """
    store = CrashingStore(crash_at, point)
    effects = Effects()
    decisions: list[Decision] = []
    crashed = 0
    clock_at = 1.0
    # The delivery sink is the outside world, so a restarted host finds what the dead one published.
    delivery = FakePublicationDelivery(store)
    while True:
        tape = Tape()
        shell, host, config, clock = _host(
            subject, world, (store, delivery, effects), tape, clock_at
        )
        try:
            start_core(host, config)
            outcome = run_virtual(clock, drive_core(host, config))
        except HostDiedError:
            decisions.extend(d for _, _, p in tape.decided for d in p.decisions)
            crashed += 1
            # The next host starts after the dead one's lease has expired.
            clock_at += LEASE + 1
            continue
        assert outcome.status == RunStatus.TERMINAL
        decisions.extend(d for _, _, p in tape.decided for d in p.decisions)
        envelope = shell.record.envelope
        return Survived(
            core=envelope.core,
            state=envelope.strategy,
            decisions=decisions,
            effects=effects,
            commits=store.commits,
            crashed=crashed,
        )


def _host(
    subject: Subject,
    world: World,
    outside: tuple[CrashingStore, FakePublicationDelivery, Effects],
    tape: Tape,
    clock_at: float,
) -> tuple[CoreRuntime, CoreRunHost, RunLoopConfig, VirtualClock]:

    store, delivery, effects = outside
    harness = subject.harness
    strategy = Taped(subject.new_strategy(), tape)
    shell: CoreRuntime = CoreRuntime(
        store,
        strategy,
        new_run(strategy, harness),
        bindings=CoreRuntimeBindings(
            registry=harness.registry,
            executors=request_executors(
                RecoverableExecutors(
                    world,
                    lambda: shell.record.envelope.core,
                    harness.registry,
                    subject.schemas,
                    effects,
                )
            ),
        ),
    )
    clock = VirtualClock(clock_at)
    host = CoreRunHost(shell, delivery, clock)
    config = RunLoopConfig(host_id="conformance", lease_duration=LEASE, max_dispatches=400)
    return shell, host, config, clock
