"""Trace driver: run a pure `Strategy` on the real core against scripted executors.

The driver stands in for the host shell. It calls the real `vs_core.api.step` and
`project`, asks the strategy for a proposal, authorizes every prepared request,
asks the scripted executors what each one observed, feeds those observations back
to core and folds every `StrategyEvent` through `Strategy.on_event`. Nothing here
decides a lifecycle fact: a missing or wrong observation shows up as core
rejecting it, never as the driver filling in the gap.

Scripts describe what an executor *saw*, not core events. The driver wraps each
`Answer` in a full `Observation` whose identity (request, scope, admission,
sequence) it derives from the request, so a script cannot forge it. Faults
(`Faults`) layer unknown first answers, retryable failures, duplicate and
reordered delivery, and a codec reload of the whole envelope between steps over
any script.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Capabilities,
    ClockAdvanced,
    ContractError,
    CoreEvent,
    CoreState,
    Decision,
    DispatchAuthorized,
    EventCursor,
    EventId,
    HostFence,
    HostId,
    IntentPhase,
    LifecycleCapability,
    Limits,
    MeasurementFailure,
    Observation,
    ObservationStatus,
    OperationRegistry,
    ProposalSubmitted,
    Request,
    RequestObserved,
    ResourceId,
    RevisionRef,
    RunEnvelope,
    RunFacts,
    RunView,
    Strategy,
    StrategyEvent,
    StrategyState,
    project,
    step,
    validate_startup,
)
from vs_core.testing.builders import initial_state

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_core.api import EvaluationTerminalFacts
    from vs_core.types.evaluation import Continuation, EvidenceRef


@dataclass(frozen=True)
class Succeeded:
    """The request completed. Fields name what the executor attached to the observation."""

    revision: RevisionRef | None = None
    resource_id: ResourceId | None = None
    outcome: BaseModel | None = None
    output_json: str | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    facts: EvaluationTerminalFacts | None = None
    suspension: Continuation | None = None


@dataclass(frozen=True)
class Running:
    """The executor accepted the request and its work is still in flight (a job handle)."""

    resource_id: ResourceId


@dataclass(frozen=True)
class Unknown:
    """The executor cannot say whether the request ran."""


@dataclass(frozen=True)
class Retryable:
    """The request failed in a way a retry may fix (a non-terminal failure)."""


@dataclass(frozen=True)
class Failed:
    """The request was refused or ran and failed for good (terminal, not accepted)."""

    measurement_failure: MeasurementFailure | None = None


type Answer = Succeeded | Running | Unknown | Retryable | Failed
"""One executor observation; a tuple of answers is a sequence of observations."""

type Script = Callable[[Request, CoreState], Answer | tuple[Answer, ...]]
"""What the executors answer for one request, given core's state when it is authorized."""


@dataclass(frozen=True)
class Faults:
    """Delivery faults the driver layers over any script.

    ``unknown_first`` and ``retry_first`` make the selected requests report an
    Unknown or a retryable failure before their scripted answer, as later
    observations of the same request. ``duplicate`` delivers each observation
    twice. ``reorder`` delivers the observations of one dispatch round newest
    first. ``reload`` round-trips the whole envelope through the registered codec
    after every step, as a restart would.
    """

    unknown_first: Callable[[Request], bool] = lambda _request: False
    retry_first: Callable[[Request], bool] = lambda _request: False
    duplicate: bool = False
    reorder: bool = False
    reload: bool = False


@dataclass
class Trace[S: StrategyState]:
    """Everything one drive saw, in order, and where it ended."""

    core: CoreState
    strategy: Strategy[S]
    decisions: list[Decision] = field(default_factory=list)
    events: list[StrategyEvent] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    finished: bool = False
    steps: int = 0

    @property
    def view(self) -> RunView:
        """The run as the strategy sees it now."""
        return project(self.core)

    def open_requests(self) -> tuple[Request, ...]:
        """Requests core holds that no executor has completed."""
        return tuple(
            intent.request
            for intent in self.core.intents.intents
            if intent.phase is not IntentPhase.COMPLETED
        )


class _Driver[S: StrategyState]:
    def __init__(
        self,
        strategy: Strategy[S],
        script: Script,
        registry: OperationRegistry,
        faults: Faults,
        core: CoreState,
    ) -> None:
        self.script = script
        self.registry = registry
        self.faults = faults
        self.trace = Trace(core=core, strategy=strategy)
        self.state: S = strategy.state
        self.clock = 1.0
        self.calls: dict[str, int] = {}

    # -- the shell loop ---------------------------------------------------

    def run(self, max_steps: int) -> None:
        while self.trace.steps < max_steps and not self.trace.finished:
            progressed = self.decide()
            progressed = self.dispatch_all() or progressed
            if not progressed:
                return

    def decide(self) -> bool:
        trace = self.trace
        proposal = trace.strategy.bind(self.state).decide(project(trace.core))
        decisions = tuple(self.registry.validate_decision(item) for item in proposal.decisions)
        trace.decisions.extend(decisions)
        changed = bool(decisions) or proposal.state != self.state
        self.state = proposal.state
        if not decisions:
            return changed
        self.consume(
            ProposalSubmitted(decisions=decisions, expected_revision=trace.core.revision),
            proposed=proposal.state,
        )
        return True

    def dispatch_all(self) -> bool:
        any_dispatched = False
        while True:
            batch = self.authorize_round()
            if not batch:
                return any_dispatched
            any_dispatched = True
            deliveries = [self.observe(request) for request in batch]
            if self.faults.reorder:
                deliveries.reverse()
            for delivery in deliveries:
                for event in delivery:
                    for _ in range(2 if self.faults.duplicate else 1):
                        self.consume(event)

    def authorize_round(self) -> list[Request]:
        """Authorize every prepared request core lets go now, in ledger order."""
        batch: list[Request] = []
        for intent in self.trace.core.intents.intents:
            if intent.phase is not IntentPhase.PREPARED:
                continue
            try:
                self.consume(DispatchAuthorized(request_id=intent.request_id))
            except ContractError as error:
                if error.path in (("dependency",), ("recovery",)):
                    continue
                raise
            batch.append(intent.request)
        return batch

    # -- executors --------------------------------------------------------

    def observe(self, request: Request) -> list[CoreEvent]:
        key = request.request_id.root if request.request_id is not None else ""
        self.trace.requests.append(request)
        answer = self.script(request, self.trace.core)
        answers = list(answer) if isinstance(answer, tuple) else [answer]
        if self.faults.retry_first(request):
            answers.insert(0, Retryable())
        if self.faults.unknown_first(request):
            answers.insert(0, Unknown())
        events: list[CoreEvent] = []
        for item in answers:
            sequence = self.calls.get(key, 0)
            self.calls[key] = sequence + 1
            observed = self.observation(request, item, sequence)
            events.append(observed)
        return events

    def observation(self, request: Request, answer: Answer, sequence: int) -> RequestObserved:
        self.clock += 1.0
        if request.request_id is None:
            message = "a dispatched request carries its identity"
            raise ValueError(message)
        status, accepted, terminal = {
            Succeeded: (ObservationStatus.SUCCEEDED, True, True),
            Running: (ObservationStatus.PENDING, True, False),
            Unknown: (ObservationStatus.UNKNOWN, False, False),
            Retryable: (ObservationStatus.FAILED, False, False),
            Failed: (ObservationStatus.FAILED, False, True),
        }[type(answer)]
        observation = Observation(
            event_id=EventId(root=f"{request.request_id.root}:observation:{sequence}"),
            request_id=request.request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=sequence,
            observed_at=self.clock,
            status=status,
            accepted=accepted,
            terminal=terminal,
            released=terminal,
            children_complete=terminal,
            resource_id=answer.resource_id if isinstance(answer, Succeeded | Running) else None,
            revision=answer.revision if isinstance(answer, Succeeded) else None,
        )
        if isinstance(answer, Failed):
            return RequestObserved(
                observation=observation, measurement_failure=answer.measurement_failure
            )
        if not isinstance(answer, Succeeded):
            return RequestObserved(observation=observation)
        outcome = answer.outcome
        operation_schema = None
        schema = None
        if outcome is not None:
            operation = getattr(request, "operation", None)
            operation_schema = None if operation is None else operation.schema_ref
            schema = None if operation_schema is None else operation_schema.outcome_schema
        elif answer.output_json is not None:
            # Session executors report a reply this way; core's ingress refuses it unless
            # it carries a registered-codec proof (see the handoff, contract gap 1).
            schema = getattr(getattr(request, "turn", None), "output_schema", None)
        event = RequestObserved(
            observation=observation,
            revision=answer.revision,
            outcome=outcome,
            outcome_schema=schema,
            operation_schema=operation_schema,
            outcome_json=answer.output_json,
            evidence=tuple(
                item.model_copy(update={"observation_sequence": sequence})
                for item in answer.evidence
            ),
            evaluation_result=answer.facts,
            suspension=answer.suspension,
        )
        return self.registry.validate_event(event) if outcome is not None else event

    # -- core -------------------------------------------------------------

    def consume(self, event: CoreEvent, *, proposed: S | None = None) -> None:
        trace = self.trace
        transition = step(trace.core, event)
        trace.steps += 1
        trace.core = transition.state
        view = project(transition.state)
        state = self.state if proposed is None else proposed
        for item in transition.events:
            trace.events.append(item)
            state = trace.strategy.bind(state).on_event(view, item)
        self.state = state
        trace.strategy = trace.strategy.bind(state)
        if transition.state.run.status.value == "terminal":
            trace.finished = True
        if self.faults.reload:
            self.reload()

    def reload(self) -> None:
        trace = self.trace
        envelope = RunEnvelope[type(self.state)](  # type: ignore[misc]
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=HostFence(host_id=HostId(root="driver"), epoch=1),
            strategy_id=trace.core.run.declaration.strategy_id,
            state_schema=trace.core.run.declaration.state_schema,
            core=trace.core,
            strategy=self.state,
            event_cursor=EventCursor(sequence=0),
        )
        loaded = self.registry.decode_envelope(
            type(envelope), self.registry.encode_envelope(envelope)
        )
        trace.core = loaded.core
        self.state = loaded.strategy
        trace.strategy = trace.strategy.bind(self.state)


@dataclass(frozen=True)
class Harness:
    """What the host around the strategy provides: codec, run facts, capabilities, limits.

    ``events`` are delivered before the first decision (operator controls, for
    example).
    """

    registry: OperationRegistry
    facts: RunFacts
    lifecycle: frozenset[LifecycleCapability] = frozenset()
    limits: Limits = field(default_factory=Limits)
    events: tuple[CoreEvent, ...] = ()
    deadline_at: float = 100000.0
    max_steps: int = 2000


def new_run(strategy: Strategy[StrategyState], harness: Harness) -> CoreState:
    """A run that has started: declaration validated against what the host offers."""
    base = initial_state()
    declaration = strategy.declaration
    registry = harness.registry
    offered = Capabilities(lifecycle=harness.lifecycle, operations=registry.descriptors)
    capabilities = validate_startup(declaration, offered)
    return base.model_copy(
        update={
            "registry": registry.descriptors,
            "run": base.run.model_copy(
                update={
                    "facts": harness.facts,
                    "declaration": declaration,
                    "capabilities": capabilities,
                    "limits": harness.limits,
                    "deadline_at": harness.deadline_at,
                }
            ),
        }
    )


def drive[S: StrategyState](
    strategy: Strategy[S], script: Script, harness: Harness, faults: Faults | None = None
) -> Trace[S]:
    """Run ``strategy`` on the real core until the run ends or nothing more happens.

    Core rejects an executor observation by raising, as the shell would halt; a
    rejected proposal is a `Rejected` strategy event in the trace. The trace holds
    every decision, request and strategy event, and the final core state.
    """
    core = new_run(strategy, harness)
    driver = _Driver(strategy, script, harness.registry, faults or Faults(), core)
    driver.consume(ClockAdvanced(now_at=1.0))
    for event in harness.events:
        driver.consume(event)
    driver.run(harness.max_steps)
    return driver.trace
