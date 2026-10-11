"""A restartable run: a disk that outlives a process, and an outside world that remembers effects.

The shell is the production one. A process dies by raising `Crash` (a `BaseException`, which no
shell code may swallow) either right after a durable commit or right after an executor's effect
ran, before its observation was committed. The next process is handed the same disk and the same
world, so it recovers exactly as a restart would: from the committed record, inspecting what it
cannot account for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.issue_queue.strategy._executors import WorldTranslator
from tests.vibesys.orchestration.issue_queue.strategy._run import config, harness

from vibesys.orchestration.issue_queue.core_policy.api import reply_schemas
from vibesys.orchestration.issue_queue.strategy.api import (
    IssueQueueStrategy,
    IssueQueueStrategyState,
)
from vs_core.api import (
    EventId,
    InspectRequest,
    Observation,
    ObservationStatus,
    ReissueProof,
    RunStatus,
    TargetObservation,
)
from vs_core.testing.drive import Succeeded, new_run
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    ExecutionResult,
    RunLoopConfig,
    as_target,
    drive_core,
    start_core,
)
from vs_runtime.api.testing import (
    LEASE,
    FakePublicationDelivery,
    RecordingStrategy,
    request_executors,
)
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, Decision, OperationRegistry, Request, SchemaRef
    from vs_runtime.api.core import ExecutionContext, OwnerEvent, RuntimeRecord
    from vs_runtime.api.testing import Script


class Crash(BaseException):
    """The process died here."""


@dataclass
class Outside:
    """What outlives a process: the store, the publication sink and what executors left behind."""

    store: FakeStateStore = field(default_factory=FakeStateStore)
    delivery: FakePublicationDelivery | None = None
    sealed: dict[str, ExecutionResult] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)
    sequences: dict[str, int] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    commits: int = 0
    deaths: int = 0
    clock_at: float = 1.0


@dataclass
class DiesAt:
    """Where a process dies: after its n-th commit, or after the n-th effect ran."""

    commit: int | None = None
    effect: int | None = None


class _Observer:
    def __init__(self, outside: Outside, dies: DiesAt) -> None:
        self._outside = outside
        self._dies = dies

    def committed(
        self,
        previous: RuntimeRecord[IssueQueueStrategyState] | None,
        current: RuntimeRecord[IssueQueueStrategyState],
    ) -> None:
        del previous, current
        self._outside.commits += 1
        if self._dies.commit is not None and self._outside.commits == self._dies.commit:
            raise Crash


class _Executors(WorldTranslator):
    """Scripted executors whose effects are remembered, so a restart inspects them."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-771106 [PLR0913]; the scripted executor's own arguments plus the process's world and death.
        self,
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        *,
        outside: Outside,
        dies: DiesAt,
    ) -> None:
        super().__init__(script, core, registry, schemas)
        self._outside = outside
        self._dies = dies

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        if isinstance(request, InspectRequest) and not self._owns(request):
            return self._inspect(request, context)
        assert request.request_id is not None
        outside = self._outside
        if not isinstance(request, InspectRequest):
            outside.executed.append(request.request_id.root)
        result = await super().execute(request, context)
        if isinstance(request, InspectRequest):
            return result
        outside.sealed[request.request_id.root] = result
        if self._dies.effect is not None and len(outside.executed) == self._dies.effect:
            raise Crash
        return result

    def _sealed_target(self, sealed: ExecutionResult) -> TargetObservation:
        """The target's facts from a sealed result, its operation outcome decoded as production does."""
        observed = sealed.observation
        schema = getattr(observed, "operation_schema", None)
        outcome = getattr(observed, "outcome", None)
        if outcome is None or schema is None:
            return as_target(observed)
        bare = observed.model_copy(
            update={"outcome": None, "outcome_schema": None, "operation_schema": None}
        )
        decoded = self._registry.decode_outcome(
            schema, self._registry.encode_outcome(schema, outcome)
        )
        return as_target(bare).model_copy(
            update={
                "outcome": decoded,
                "operation_schema": schema,
                "outcome_schema": schema.outcome_schema,
            }
        )

    def _inspect(self, request: InspectRequest, context: ExecutionContext) -> ExecutionResult:
        """What a restarted process learns of a request: its sealed result, or that it must run again."""
        outside = self._outside
        target = request.target
        sealed = outside.sealed.get(target.root)
        owner_events: tuple[OwnerEvent, ...] = ()
        if sealed is not None:
            facts = self._sealed_target(sealed)
            owner_events = sealed.owner_events
        else:
            began = target.root in outside.executed
            sequence = outside.sequences.get(target.root, 0)
            outside.sequences[target.root] = sequence + 1
            observation = Observation(
                event_id=EventId(root=f"{target.root}:observation:{sequence}"),
                request_id=target,
                scope=request.scope,
                admission_id=request.admission_id,
                sequence=sequence,
                observed_at=context.now_at,
                status=ObservationStatus.UNKNOWN if began else ObservationStatus.REJECTED,
                accepted=False,
                terminal=not began,
                released=False,
                children_complete=False,
            )
            facts = TargetObservation(
                observation=observation,
                reissue=ReissueProof.BEGUN_UNSEALED if began else ReissueProof.NEVER_BEGAN,
            )
        own = self._observed(request, Succeeded(), context.now_at)
        return ExecutionResult(
            observation=own.model_copy(update={"target": facts}), owner_events=owner_events
        )


@dataclass(frozen=True)
class Process:
    """One process of the run."""

    shell: CoreRuntime
    host: CoreRunHost
    config: RunLoopConfig
    clock: VirtualClock


def start(script: Script, outside: Outside, dies: DiesAt, **overrides: object) -> Process:
    """A process over ``outside``: a fresh strategy instance bound to what the store holds."""
    settings = config(**overrides)
    chosen = harness(settings)
    strategy = IssueQueueStrategy(config=settings)
    recording = RecordingStrategy(strategy, outside.decisions)
    shell: CoreRuntime = CoreRuntime(
        outside.store,
        recording,
        new_run(strategy, chosen),
        bindings=CoreRuntimeBindings(
            registry=chosen.registry,
            commits=_Observer(outside, dies),
            executors=request_executors(
                _Executors(
                    script,
                    lambda: shell.record.envelope.core,
                    chosen.registry,
                    reply_schemas(settings),
                    outside=outside,
                    dies=dies,
                )
            ),
        ),
    )
    clock = VirtualClock(outside.clock_at)
    if outside.delivery is None:
        outside.delivery = FakePublicationDelivery(outside.store)
    host = CoreRunHost(shell, outside.delivery, clock)
    loop_config = RunLoopConfig(host_id="crash", lease_duration=LEASE, max_dispatches=600)
    start_core(host, loop_config)
    return Process(shell, host, loop_config, clock)


def run_once(
    script: Script, outside: Outside, dies: DiesAt, **overrides: object
) -> CoreState | None:
    """Run one process to its end, or to its death; return core as last committed, if it got that far."""
    try:
        process = start(script, outside, dies, **overrides)
        run_virtual(process.clock, drive_core(process.host, process.config))
    except Crash:
        outside.deaths += 1
        outside.clock_at += 4 * LEASE
        return None
    return process.shell.record.envelope.core


def run_with_restarts(
    script: Script, outside: Outside, dies: list[DiesAt], **overrides: object
) -> CoreState:
    """Run processes that die where ``dies`` say, then one that is left to finish."""
    for point in dies:
        outside.commits = 0
        core = run_once(script, outside, point, **overrides)
        if core is not None and core.run.status == RunStatus.TERMINAL:
            return core
    outside.commits = 0
    core = run_once(script, outside, DiesAt(), **overrides)
    assert core is not None
    return core
