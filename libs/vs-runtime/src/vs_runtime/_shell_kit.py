"""Run a strategy through the production shell with scripted executors.

The loop, the lease, the fence, the clock, the durable commit and the owner-event
queue are the real `vs_runtime` ones (`CoreRuntime` driven by `drive_core`), so the
event protocol cannot drift from production. Only the executors at the far end are
scripted: `ScriptedExecutors` turns what a script says an executor saw (an `Answer`)
into the `ExecutionResult` a production translator returns for that request, owner
events included. A script never builds an event, and nothing here decides a
lifecycle fact: a missing or wrong observation shows up as core rejecting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ValidationError

from vs_core.api import (
    AdoptionObserved,
    AdoptRevision,
    CancelTurn,
    CloseSession,
    CollectEvidence,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    EventId,
    FlowState,
    InvocationRef,
    JobObserved,
    LifecycleCapability,
    MeasurementSubmissionObserved,
    Observation,
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    ReplyError,
    RequestObserved,
    RestoreRevision,
    ResumeSessionTurn,
    SessionObserved,
    SubmitMeasurement,
    TurnObserved,
    VerifyAdoption,
    WorkspaceObserved,
)
from vs_core.api.testing import (
    Failed,
    Harness,
    Journal,
    Retryable,
    Running,
    Succeeded,
    Unknown,
    new_run,
)
from vs_project.api import FakeStateStore
from vs_runtime._fake_core_execution import FakePublicationDelivery
from vs_runtime._observation_factory import ObservationFacts
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    DispatchCapExceededError,
    ExecutionResult,
    RequestExecutors,
    RunControlBridge,
    RunLoopConfig,
    RunStalledError,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RunStopped, create_run_control_channel
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import (
        CoreEvent,
        CoreState,
        Decision,
        Event,
        OperationRegistry,
        Request,
        ResourceId,
        RunFacts,
        RunView,
        SchemaRef,
        SearchDeclaration,
        SearchDef,
        SearchDriver,
        StrategyState,
        Transition,
    )
    from vs_core.api.recovery import OwnerEvent
    from vs_core.api.testing import Answer
    from vs_runtime._step_loop import StepLoopStalledError
    from vs_runtime.api.core import ExecutionContext


@runtime_checkable
class Admitting(Protocol):
    """A script whose executors call into the shell, as the agent evaluation bridge does."""

    admit: Callable[[CoreEvent, float], Transition] | None


type Script = Callable[[Request, CoreState], Answer]
"""What the executors answer for one request, given core's state when it runs."""


class UnidentifiedRequestError(RuntimeError):
    """A request reached the scripted executors without the identity core assigns it."""

    def __init__(self) -> None:
        super().__init__("a dispatched request carries its identity")


@dataclass(frozen=True)
class InvalidReply:
    """The turn ran and its reply does not parse as the schema it was asked to answer."""

    resource_id: ResourceId | None
    errors: tuple[ReplyError, ...] = ()


def _status(answer: Answer | InvalidReply) -> tuple[ObservationStatus, bool, bool]:
    """Status, accepted and terminal of the observation an answer stands for."""
    if isinstance(answer, InvalidReply):
        return ObservationStatus.FAILED, True, True
    if isinstance(answer, Succeeded):
        return ObservationStatus.SUCCEEDED, True, True
    if isinstance(answer, Running):
        return ObservationStatus.PENDING, True, False
    if isinstance(answer, Unknown):
        return ObservationStatus.UNKNOWN, False, False
    if isinstance(answer, Retryable):
        return ObservationStatus.FAILED, False, False
    return ObservationStatus.FAILED, answer.accepted, True


def _reports_job(answer: Answer | InvalidReply) -> bool:
    """Whether an executor that took a job request reports the job's own state with it."""
    return isinstance(answer, Succeeded | Running) or (
        isinstance(answer, Failed) and answer.accepted
    )


def _lifecycle_event(request: Request, observed: RequestObserved) -> OwnerEvent | None:
    """The owner event of a session, workspace or adoption request, none for other requests."""
    view = observed.observation
    match request:
        case EnsureSession():
            return SessionObserved(session_id=request.spec.session_id, observation=view)
        case CloseSession():
            return SessionObserved(session_id=request.session_id, observation=view)
        case AdoptRevision() | VerifyAdoption():
            return AdoptionObserved(observation=view, revision=observed.revision)
        case EnsureWorkspace() | RestoreRevision():
            return WorkspaceObserved(
                attempt=request.attempt, observation=view, revision=observed.revision
            )
        case _:
            return None


class ScriptedExecutors:
    """Every executor role, answering from one script as the production translators do."""

    def __init__(
        self,
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal | None = None,
    ) -> None:
        """Bind the script, a read of core's state, the operation codec and the reply types.

        ``journal`` records every request the executors receive and every observation they return.
        """
        self.journal = journal if journal is not None else Journal()
        self._script = script
        self._core = core
        self._registry = registry
        self._schemas = schemas
        self._sequences: dict[str, int] = {}

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """What the executor reports for ``request``: one observation and its owner events."""
        self.journal.issue(request)
        answer = self._parsed(request, self._script(request, self._core()))
        observed = self._observed(request, answer, context.now_at)
        owner_events = self._owner_events(request, answer, observed)
        if isinstance(request, ObserveOwnedJob) and isinstance(answer, Running):
            # A poll is a query: it completes with the job's state, and the state it found is
            # the job's own observation. The job is still running; the poll is not.
            owner_events = (self._job_event(request, observed),)
            observed = self._observed(request, Succeeded(), context.now_at)
        self.journal.observe_event(observed)
        for event in owner_events:
            if isinstance(event, JobObserved):
                self.journal.observe(event.observation, event.evidence)
        return ExecutionResult(observation=observed, owner_events=owner_events)

    def _parsed(self, request: Request, answer: Answer) -> Answer | InvalidReply:
        """A turn's reply is what the schema parses it to, as the session executor does."""
        if (
            not isinstance(request, DispatchTurn | ResumeSessionTurn)
            or not isinstance(answer, Succeeded)
            or answer.output_json is None
        ):
            return answer
        schema = self._schemas[request.turn.output_schema]
        try:
            reply = schema.model_validate_json(answer.output_json)
        except ValidationError:
            return InvalidReply(answer.resource_id)
        return replace(answer, output_json=reply.model_dump_json())

    def _next(self, key: str) -> int:
        sequence = self._sequences.get(key, 0)
        self._sequences[key] = sequence + 1
        return sequence

    def _observed(
        self, request: Request, answer: Answer | InvalidReply, now_at: float
    ) -> RequestObserved:
        if request.request_id is None:
            raise UnidentifiedRequestError
        key = request.request_id.root
        sequence = self._next(key)
        status, accepted, terminal = _status(answer)
        if isinstance(request, CancelTurn) and isinstance(answer, Succeeded):
            # A stopped turn reports cancelled and released, as the session executor does.
            status = ObservationStatus.CANCELLED
        succeeded = isinstance(answer, Succeeded)
        resource_id = (
            answer.resource_id if isinstance(answer, Succeeded | Running | InvalidReply) else None
        )
        if terminal and not accepted and resource_id is None:
            # A refusal holds nothing, so it is built as the production executors build it.
            facts = ObservationFacts.refused("")
        else:
            facts = ObservationFacts(
                status=status,
                accepted=accepted,
                terminal=terminal,
                released=terminal,
                # An accepted submission reports its job complete as a child, as production does.
                children_complete=terminal or (accepted and isinstance(request, SubmitMeasurement)),
                resource_id=resource_id,
                revision=answer.revision if succeeded else None,
            )
        observation = Observation(
            event_id=EventId(root=f"{key}:observation:{sequence}"),
            request_id=request.request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=sequence,
            observed_at=now_at,
            status=facts.status,
            accepted=facts.accepted,
            terminal=facts.terminal,
            released=facts.released,
            children_complete=facts.children_complete,
            resource_id=facts.resource_id,
            revision=facts.revision,
        )
        if isinstance(answer, Failed):
            return RequestObserved(
                observation=observation, measurement_failure=answer.measurement_failure
            )
        if not isinstance(answer, Succeeded):
            return RequestObserved(observation=observation)
        operation = getattr(request, "operation", None)
        operation_schema = None if operation is None else operation.schema_ref
        event = RequestObserved(
            observation=observation,
            revision=answer.revision,
            outcome=answer.outcome,
            outcome_schema=(
                None
                if answer.outcome is None or operation_schema is None
                else operation_schema.outcome_schema
            ),
            operation_schema=None if answer.outcome is None else operation_schema,
            evidence=tuple(
                item.model_copy(update={"observation_sequence": sequence})
                for item in answer.evidence
            ),
            evaluation_result=answer.facts,
            suspension=answer.suspension,
        )
        return self._registry.validate_event(event) if answer.outcome is not None else event

    def _owner_events(
        self, request: Request, answer: Answer | InvalidReply, observed: RequestObserved
    ) -> tuple[OwnerEvent, ...]:
        """The events a production translator attaches to the observation of ``request``."""
        match request:
            case DispatchTurn() | ResumeSessionTurn():
                return (self.turn_event(request, answer, observed),)
            case SubmitMeasurement():
                return self._submission_events(answer, observed)
            case ObserveOwnedJob() | CollectEvidence() if _reports_job(answer):
                return (self._job_event(request, observed),)
            case _:
                event = _lifecycle_event(request, observed)
                return () if event is None else (event,)

    @staticmethod
    def turn_event(
        request: DispatchTurn | ResumeSessionTurn,
        answer: Answer | InvalidReply,
        observed: RequestObserved,
    ) -> TurnObserved:
        """A turn's reply travels on the session owner's event, never on the request's own."""
        reply = answer.output_json if isinstance(answer, Succeeded) else None
        return TurnObserved(
            invocation=InvocationRef(
                session_id=request.turn.session.session_id,
                invocation_id=request.turn.invocation_id,
                generation=request.scope.generation,
            ),
            observation=observed.observation,
            output_schema=None if reply is None else request.turn.output_schema,
            output_json=reply,
            reply_errors=answer.errors if isinstance(answer, InvalidReply) else (),
        )

    @staticmethod
    def _submission_events(
        answer: Answer | InvalidReply, observed: RequestObserved
    ) -> tuple[MeasurementSubmissionObserved | JobObserved, ...]:
        """The submission's own view, which is also the job's first observation."""
        view = observed.observation
        classified = MeasurementSubmissionObserved(
            observation=view, failure=observed.measurement_failure
        )
        if not view.accepted or view.resource_id is None:
            return (classified,)
        facts = answer.facts if isinstance(answer, Succeeded) else None
        return (
            classified,
            JobObserved(
                resource_id=view.resource_id,
                observation=view,
                evidence=observed.evidence,
                evaluation_result=facts,
            ),
        )

    def _job_event(
        self, request: ObserveOwnedJob | CollectEvidence, observed: RequestObserved
    ) -> JobObserved:
        """The job's own observation, carrying its submission's identity as production does."""
        job = next(
            row
            for row in self._core().evaluation.jobs
            if isinstance(row, OwnedJob) and row.resource_id == request.resource_id
        )
        key = job.submission_id.root
        sequence = self._next(key)
        # A collection reports the job as it stands, not the collection's own outcome.
        source = job.observation if isinstance(request, CollectEvidence) else None
        view = (source or observed.observation).model_copy(
            update={
                "event_id": EventId(root=f"{key}:observation:{sequence}"),
                "request_id": job.submission_id,
                "sequence": sequence,
                "observed_at": observed.observation.observed_at,
                "resource_id": request.resource_id,
            }
        )
        return JobObserved(
            resource_id=request.resource_id,
            observation=view,
            evidence=tuple(
                item.model_copy(update={"observation_sequence": sequence})
                for item in observed.evidence
            ),
            evaluation_result=observed.evaluation_result,
        )


@dataclass(frozen=True)
class Launch[C]:
    """A search and the config its run starts from."""

    search: SearchDef[C]
    config: C


class RecordingSearch[S: StrategyState, C]:
    """A search that keeps every decision it returned, in order, for the test to read."""

    def __init__(self, inner: SearchDriver[S, C], decisions: list[Decision]) -> None:
        """Wrap ``inner``."""
        self._inner = inner
        self._decisions = decisions

    @property
    def declaration(self) -> SearchDeclaration:
        """The wrapped search's declaration."""
        return self._inner.declaration

    @property
    def state_model(self) -> type[S]:
        """The wrapped search's state class."""
        return self._inner.state_model

    def init(self, config: C, facts: RunFacts, view: RunView) -> tuple[S, list[Decision]]:
        """The wrapped search's first state and decisions, recorded."""
        state, decisions = self._inner.init(config, facts, view)
        self._decisions.extend(decisions)
        return state, decisions

    def update(self, state: S, event: Event, view: RunView) -> tuple[S, list[Decision]]:
        """The wrapped search's next state and decisions, recorded."""
        next_state, decisions = self._inner.update(state, event, view)
        self._decisions.extend(decisions)
        return next_state, decisions

    def awaits(self, state: S, decision: Decision) -> bool:
        """Whether the wrapped search in ``state`` still waits for ``decision``."""
        return self._inner.awaits(state, decision)

    def changes(self, before: S, after: S) -> bytes | None:
        """The wrapped search's change from ``before`` to ``after``."""
        return self._inner.changes(before, after)

    def extend(self, state: S, changes: bytes) -> S:
        """The wrapped search's ``state`` with ``changes`` applied."""
        return self._inner.extend(state, changes)

    def resume(self, state: S) -> S:
        """The wrapped search's ``state`` as its logging process left it."""
        return self._inner.resume(state)


@dataclass
class Run:
    """What one whole run did: the decisions proposed and the final core state."""

    core: CoreState
    # The strategy state as the shell last persisted it.
    state: StrategyState
    decisions: list[Decision] = field(default_factory=list)
    journal: Journal = field(default_factory=Journal)
    # The loop gave up on the run: its dispatch cap was hit (it never went idle) or it
    # stalled (open work and nothing to wake it).
    halted: DispatchCapExceededError | RunStalledError | StepLoopStalledError | None = None
    # An operator stop interrupted the run: it stays open for a resume (`RunStopped`).
    stopped: bool = False


LEASE = 1000.0
MAX_DISPATCHES = 400


type TranslatorFactory = Callable[
    [
        Script,
        Callable[[], CoreState],
        OperationRegistry,
        Mapping[SchemaRef, type[BaseModel]],
        Journal,
    ],
    ScriptedExecutors,
]
"""Builds the executors of a host from the script, a read of core's state, the operation codec,
the reply types and the journal; a `ScriptedExecutors` subclass is one."""


@dataclass(frozen=True)
class HostOptions:
    """How the production host around the strategy is arranged for one run."""

    max_concurrent: int = 1
    """Requests the loop executes at once."""
    translator: TranslatorFactory = ScriptedExecutors
    """The executors' factory; a test whose turns reach a real host service subclasses
    `ScriptedExecutors` and passes the class."""
    attach: Callable[[CoreRuntime[Any], VirtualClock], None] | None = None
    """Hears the shell and clock once they exist."""
    controlled: bool = False
    """Give the loop an idle run-control channel, as a product run has, so it looks at the
    shell every control-poll interval while requests run."""
    lifecycle: frozenset[LifecycleCapability] = frozenset()
    """Lifecycle capabilities the run offers its strategy (such as ``suspend``)."""
    resumed: bool = False
    """The process was started by the operator's explicit resume of the run, so its controls
    lift the stop the run's record holds (and reopen a run that stop ended)."""


@dataclass
class ScriptedHost:
    """One production host (shell, loop config, clock) over a store, ready to run."""

    shell: CoreRuntime[FlowState]
    host: CoreRunHost
    config: RunLoopConfig
    clock: VirtualClock
    decisions: list[Decision]
    journal: Journal

    def run(self) -> Run:
        """Start the host over its store (a new run, or the one the store holds) and drive it.

        An exception that is not the loop's own backstop or an operator stop (a host that
        died, for one) propagates.
        """
        start_core(self.host, self.config)
        halted = None
        stopped = False
        try:
            run_virtual(self.clock, drive_core(self.host, self.config))
        except RunStopped:
            stopped = True
        except (DispatchCapExceededError, RunStalledError) as error:
            # These are the loop's backstops. Return the run so the liveness check names the
            # invariant it broke and the requests that led there.
            halted = error
        return Run(
            core=self.shell.record.envelope.core,
            state=self.shell.record.envelope.strategy,
            decisions=self.decisions,
            journal=self.journal,
            halted=halted,
            stopped=stopped,
        )


def scripted_host[C](  # noqa: PLR0913  # lint-waiver: LW-994707 [PLR0913]; a restarted host inherits the dead one's store, delivery and start time beside the run's description.
    # > Bundling the three inherited values into one holder adds a type that only this signature uses, and splitting the function would repeat the shell wiring it exists to share.
    launch: Launch[C],
    script: Script,
    harness: Harness,
    schemas: Mapping[SchemaRef, type[BaseModel]],
    host_options: HostOptions | None = None,
    *,
    store: FakeStateStore | None = None,
    delivery: FakePublicationDelivery | None = None,
    clock_at: float = 1.0,
) -> ScriptedHost:
    """A production shell and loop over a search with every executor answered by ``script``.

    ``store`` and ``delivery`` default to new ones; pass the ones a dead host left to start its
    successor over them. ``clock_at`` is the new host's virtual time.
    """
    options = host_options or HostOptions()
    decisions: list[Decision] = []
    search = launch.search
    driver = search.driver(launch.config, harness.registry)
    recorded = RecordingSearch(driver, decisions)
    store = FakeStateStore() if store is None else store
    journal = Journal()
    shell: CoreRuntime[FlowState] = CoreRuntime(
        store,
        recorded,
        new_run(driver, harness),
        config=launch.config,
        bindings=CoreRuntimeBindings(
            registry=harness.registry,
            executors=request_executors(
                options.translator(
                    script, lambda: shell.record.envelope.core, harness.registry, schemas, journal
                )
            ),
        ),
    )
    if isinstance(script, Admitting):
        script.admit = lambda event, now_at: shell.admit(event, now_at=now_at)
    clock = VirtualClock(clock_at)
    if options.attach is not None:
        options.attach(shell, clock)
    controls = (
        RunControlBridge(
            create_run_control_channel(_IgnoredTransitions(), SimThreads()),
            _NoSteer(),
            lifts_standing_stop=options.resumed,
        )
        if options.controlled or options.resumed
        else None
    )
    host = CoreRunHost(
        shell, FakePublicationDelivery() if delivery is None else delivery, clock, controls
    )
    config = RunLoopConfig(
        host_id="scenario",
        lease_duration=LEASE,
        max_dispatches=MAX_DISPATCHES,
        max_concurrent=options.max_concurrent,
    )
    return ScriptedHost(shell, host, config, clock, decisions, journal)


def drive_shell[C](
    launch: Launch[C],
    script: Script,
    harness: Harness,
    schemas: Mapping[SchemaRef, type[BaseModel]],
    host_options: HostOptions | None = None,
) -> Run:
    """Run a search to the end of its run on the production shell and loop."""
    return scripted_host(launch, script, harness, schemas, host_options).run()


class _IgnoredTransitions:
    """A run-control event sink for a channel nobody records."""

    def __call__(self, transition: object) -> None:
        del transition


class _NoSteer:
    """A steer store for a channel that never carries steer text."""

    def put(self, ref: object, text: str) -> None:
        raise AssertionError((ref, text))


def request_executors(executor: ScriptedExecutors) -> RequestExecutors:
    return RequestExecutors(
        workspaces=executor,  # type: ignore[arg-type]  # one scripted object serves every role
        sessions=executor,  # type: ignore[arg-type]  # the same scripted object serves this role
        evaluation=executor,  # type: ignore[arg-type]  # the same scripted object serves this role
        operations=executor,  # type: ignore[arg-type]  # the same scripted object serves this role
        semantic_events=executor,  # type: ignore[arg-type]  # the same scripted object serves this role
    )
