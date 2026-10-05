"""Run `DynamicStrategy` through the production shell with scripted executors.

The loop, the lease, the fence, the clock, the durable commit and the owner-event
queue are the real `vs_runtime` ones (`CoreRuntime` driven by `drive_core`), so the
event protocol cannot drift from production. Only the executors at the far end are
scripted: `ScriptedExecutors` turns what a script says an executor saw (an `Answer`)
into the `ExecutionResult` a production translator returns for that request, owner
events included. A script never builds an event, and nothing here decides a
lifecycle fact: a missing or wrong observation shows up as core rejecting it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.support.fake_run_clock import FakeRunClock

from vs_core.api import (
    AdoptionObserved,
    AdoptRevision,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    EventId,
    InvocationRef,
    JobObserved,
    MeasurementSubmissionObserved,
    Observation,
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    Proposal,
    RequestObserved,
    RestoreRevision,
    ResumeSessionTurn,
    SessionObserved,
    SubmitMeasurement,
    TurnObserved,
    VerifyAdoption,
    WorkspaceObserved,
)
from vs_core.testing.drive import Failed, Harness, Retryable, Running, Succeeded, Unknown, new_run
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    ExecutionResult,
    RequestExecutors,
    RunLoopConfig,
    drive_core,
    start_core,
)
from vs_runtime.api.testing import FakePublicationDelivery

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import (
        CoreState,
        Decision,
        OperationRegistry,
        OwnerEvent,
        Request,
        RunView,
        Strategy,
        StrategyEvent,
        StrategyState,
    )
    from vs_core.testing.drive import Answer
    from vs_runtime.api.core import ExecutionContext

type Script = Callable[[Request, CoreState], Answer]
"""What the executors answer for one request, given core's state when it runs."""


def _status(answer: Answer) -> tuple[ObservationStatus, bool, bool]:
    """Status, accepted and terminal of the observation an answer stands for."""
    if isinstance(answer, Succeeded):
        return ObservationStatus.SUCCEEDED, True, True
    if isinstance(answer, Running):
        return ObservationStatus.PENDING, True, False
    if isinstance(answer, Unknown):
        return ObservationStatus.UNKNOWN, False, False
    if isinstance(answer, Retryable):
        return ObservationStatus.FAILED, False, False
    return ObservationStatus.FAILED, False, True


class ScriptedExecutors:
    """Every executor role, answering from one script as the production translators do."""

    def __init__(
        self, script: Script, core: Callable[[], CoreState], registry: OperationRegistry
    ) -> None:
        """Bind the script, a read of the run's current core state and the operation codec."""
        self._script = script
        self._core = core
        self._registry = registry
        self._sequences: dict[str, int] = {}

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """What the executor reports for ``request``: one observation and its owner events."""
        answer = self._script(request, self._core())
        observed = self._observed(request, answer, context.now_at)
        return ExecutionResult(
            observation=observed, owner_events=self._owner_events(request, answer, observed)
        )

    def _next(self, key: str) -> int:
        sequence = self._sequences.get(key, 0)
        self._sequences[key] = sequence + 1
        return sequence

    def _observed(self, request: Request, answer: Answer, now_at: float) -> RequestObserved:
        assert request.request_id is not None, "a dispatched request carries its identity"
        key = request.request_id.root
        sequence = self._next(key)
        status, accepted, terminal = _status(answer)
        succeeded = isinstance(answer, Succeeded)
        observation = Observation(
            event_id=EventId(root=f"{key}:observation:{sequence}"),
            request_id=request.request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=sequence,
            observed_at=now_at,
            status=status,
            accepted=accepted,
            terminal=terminal,
            released=terminal,
            children_complete=terminal,
            resource_id=answer.resource_id if isinstance(answer, Succeeded | Running) else None,
            revision=answer.revision if succeeded else None,
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
        self, request: Request, answer: Answer, observed: RequestObserved
    ) -> tuple[OwnerEvent, ...]:
        if isinstance(request, DispatchTurn | ResumeSessionTurn):
            return (self._turn_event(request, answer, observed),)
        if isinstance(request, AdoptRevision | VerifyAdoption):
            return (AdoptionObserved(observation=observed.observation, revision=observed.revision),)
        if isinstance(request, EnsureWorkspace | RestoreRevision):
            return (
                WorkspaceObserved(
                    attempt=request.attempt,
                    observation=observed.observation,
                    revision=observed.revision,
                ),
            )
        if isinstance(request, EnsureSession):
            return (SessionObserved(session_id=request.spec.session_id, observation=observed.observation),)
        if isinstance(request, CloseSession):
            return (SessionObserved(session_id=request.session_id, observation=observed.observation),)
        if isinstance(request, SubmitMeasurement):
            return self._submission_events(answer, observed)
        if isinstance(request, ObserveOwnedJob) and isinstance(answer, Succeeded):
            return (self._job_event(request, observed),)
        return ()

    @staticmethod
    def _turn_event(
        request: DispatchTurn | ResumeSessionTurn, answer: Answer, observed: RequestObserved
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
        )

    @staticmethod
    def _submission_events(
        answer: Answer, observed: RequestObserved
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

    def _job_event(self, request: ObserveOwnedJob, observed: RequestObserved) -> JobObserved:
        """The job's own observation, carrying its submission's identity as production does."""
        job = next(
            row
            for row in self._core().evaluation.jobs
            if isinstance(row, OwnedJob) and row.resource_id == request.resource_id
        )
        key = job.submission_id.root
        sequence = self._next(key)
        view = observed.observation.model_copy(
            update={
                "event_id": EventId(root=f"{key}:observation:{sequence}"),
                "request_id": job.submission_id,
                "sequence": sequence,
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


class RecordingStrategy[S: StrategyState]:
    """A strategy that keeps every decision it proposed, in order, for the test to read."""

    def __init__(self, inner: Strategy[S], decisions: list[Decision]) -> None:
        """Wrap ``inner``; every binding shares one list."""
        self._inner = inner
        self._decisions = decisions
        self.declaration = inner.declaration

    @property
    def state(self) -> S:
        """The wrapped strategy's state."""
        return self._inner.state

    def bind(self, state: S) -> RecordingStrategy[S]:
        """The same strategy over another state."""
        return RecordingStrategy(self._inner.bind(state), self._decisions)

    def on_event(self, view: RunView, event: StrategyEvent) -> S:
        """The wrapped strategy's fold."""
        return self._inner.on_event(view, event)

    def decide(self, view: RunView) -> Proposal[S]:
        """The wrapped strategy's proposal, recorded."""
        proposal = self._inner.decide(view)
        self._decisions.extend(proposal.decisions)
        return proposal


@dataclass
class Run:
    """What one whole run did: the decisions proposed and the final core state."""

    core: CoreState
    decisions: list[Decision] = field(default_factory=list)


LEASE = 1000.0
MAX_DISPATCHES = 400


def drive_shell[S: StrategyState](
    strategy: Strategy[S], script: Script, harness: Harness
) -> Run:
    """Run ``strategy`` to the end of its run on the production shell and loop."""
    decisions: list[Decision] = []
    recorded = RecordingStrategy(strategy, decisions)
    store = FakeStateStore()
    shell: CoreRuntime[S] = CoreRuntime(
        store,
        recorded,
        new_run(strategy, harness),
        bindings=CoreRuntimeBindings(
            registry=harness.registry,
            executors=_executors(
                ScriptedExecutors(script, lambda: shell.record.envelope.core, harness.registry)
            ),
        ),
    )
    clock = FakeRunClock(1.0)
    host = CoreRunHost(shell, FakePublicationDelivery(store), clock)
    config = RunLoopConfig(host_id="scenario", lease_duration=LEASE, max_dispatches=MAX_DISPATCHES)
    start_core(host, config)
    asyncio.run(drive_core(host, config))
    return Run(core=shell.record.envelope.core, decisions=decisions)


def _executors(executor: ScriptedExecutors) -> RequestExecutors:
    return RequestExecutors(
        workspaces=executor,  # type: ignore[arg-type]  # one scripted object serves every role
        sessions=executor,  # type: ignore[arg-type]
        evaluation=executor,  # type: ignore[arg-type]
        operations=executor,  # type: ignore[arg-type]
        semantic_events=executor,  # type: ignore[arg-type]
    )
