"""Trace driver: run a search driver on the real core against scripted executors.

The driver stands in for the host shell. It calls the real `vs_core.api.step` and
`project`, hands the search every event core publishes, authorizes every prepared
request, asks the scripted executors what each one observed and feeds those
observations back to core. When nothing else can happen it moves the clock to the next
time that matters (a requested wake-up or a request's limit). Nothing here
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

from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    AdoptionObserved,
    AdoptRevision,
    BlockRequest,
    CancelOwnedResource,
    Capabilities,
    ClockAdvanced,
    CloseSession,
    CollectEvidence,
    ContractError,
    CoreEvent,
    CoreReducers,
    CoreState,
    CoreView,
    Decision,
    Discarded,
    DispatchAuthorized,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    Event,
    EventCursor,
    EventId,
    EvidenceRequirements,
    HostFence,
    HostId,
    Inbox,
    InspectRequest,
    InspectTurn,
    InvocationRef,
    JobObserved,
    LifecycleCapability,
    Limits,
    MeasurementFailure,
    MeasurementSubmissionObserved,
    Observation,
    ObservationStatus,
    ObserveOwnedJob,
    OperationRegistry,
    OwnedJob,
    ReissueProof,
    ReplyError,
    Request,
    RequestObserved,
    RequestPhase,
    ResourceId,
    RestoreRevision,
    ResumeSessionTurn,
    RevisionRef,
    RunEnvelope,
    RunFacts,
    SearchDriver,
    SearchHost,
    SessionObserved,
    StrategyEvent,
    StrategyState,
    Submission,
    SubmitMeasurement,
    TargetObservation,
    TimeReached,
    Transition,
    TurnObserved,
    VerifyAdoption,
    WorkspaceObserved,
    advance,
    begin,
    project,
    reply_errors,
    step,
    translate,
    validate_startup,
)
from vs_core.testing.builders import initial_state, sample_limits
from vs_core.testing.limits import end_one, next_end

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import EvaluationTerminalFacts, RequestId, SchemaRef
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
    measured_input: str | None = None
    """What the evaluation reads at the revision, as a workspace executor digests it: the
    attempt's base input, which a comparison with its candidates needs."""


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
    """The request ran and failed for good (terminal).

    The executor did not accept a refused request. With ``accepted`` the executor took the
    request and the job it owns ended failed, as a scheduler reports a failed batch job.
    """

    measurement_failure: MeasurementFailure | None = None
    accepted: bool = False


@dataclass(frozen=True)
class _InvalidReply:
    """The turn ran and its reply does not parse as the schema it was asked to answer."""

    resource_id: ResourceId | None
    errors: tuple[ReplyError, ...]
    """What the executor's schema check found wrong, by field."""


type Answer = Succeeded | Running | Unknown | Retryable | Failed


def _ends(observation: Observation) -> bool:
    """Whether ``observation`` reports that its job has ended (terminal, with a known status)."""
    return observation.terminal and observation.status not in (
        ObservationStatus.PENDING,
        ObservationStatus.UNKNOWN,
    )


"""One executor observation; a tuple of answers is a sequence of observations."""

type Script = Callable[[Request, CoreState], Answer | tuple[Answer, ...]]
"""What the executors answer for one request, given core's state when it is authorized."""


def _shape(answer: Answer | _InvalidReply) -> tuple[ObservationStatus, bool, bool]:
    """Status, accepted and terminal of the observation an answer stands for."""
    match answer:
        case _InvalidReply():
            return ObservationStatus.FAILED, True, True
        case Succeeded():
            return ObservationStatus.SUCCEEDED, True, True
        case Running():
            return ObservationStatus.PENDING, True, False
        case Unknown():
            return ObservationStatus.UNKNOWN, False, False
        case Retryable():
            return ObservationStatus.FAILED, False, False
        case Failed():
            return ObservationStatus.FAILED, answer.accepted, True


@dataclass(frozen=True)
class Faults:
    """Delivery faults the driver layers over any script.

    ``unknown_first`` and ``retry_first`` make the selected requests report an
    Unknown or a retryable failure before their scripted answer, as later
    observations of the same request. ``duplicate`` delivers each observation
    twice. ``reorder`` delivers the observations of one dispatch round newest
    first. ``reload`` round-trips the whole envelope through the registered codec
    after every step, as a restart would.

    ``reobserve_after`` lists commit numbers (counted as for ``crash_after``) after which the
    executor answers again for every job that has ended: a poll that was in flight when the job
    ended reports the same end, at a newer sequence, event and time.

    ``replay_after`` lists commit numbers (counted as for ``crash_after``) after which the
    job executors deliver again, oldest first, every answer to a job poll they delivered
    before, as an executor that resends its answers after a restart does. An answer delivered
    long ago may name a poll the ledger no longer holds (a spent poll), which must change
    nothing.

    ``crash_after`` lists commit numbers (counted from the first, over the whole drive:
    every authorization, every delivery of a request's observations and its owner events, every
    clock move, and the recovery's own) after which the host dies and a new one takes over. The
    new host reloads the envelope, starts recovery under the next epoch and answers core's
    inspections from what the executors did (see ``_Executed``): a crash number that falls inside
    a recovery is a crash during recovery. A request that was executed but whose result was not
    delivered died before sealing it when ``unsealed`` says so, else its result is sealed.
    """

    unknown_first: Callable[[Request], bool] = lambda _request: False
    retry_first: Callable[[Request], bool] = lambda _request: False
    duplicate: bool = False
    reorder: bool = False
    reload: bool = False
    crash_after: frozenset[int] = frozenset()
    unsealed: Callable[[Request], bool] = lambda _request: False
    reobserve_after: frozenset[int] = frozenset()
    replay_after: frozenset[int] = frozenset()


class _CrashError(Exception):
    """The host died; the drive resumes in a new host."""


@dataclass
class _Executed:
    """What the executors did for one request: the ground truth recovery inspects.

    ``answers`` are what the effect reported (the effect happened once, whatever is delivered).
    ``events`` are the observations and owner events built from them; they exist once the result
    is sealed. A request begun whose result was never sealed has ``sealed`` False: inspecting it
    says only that it may have run, and sending it again builds its events from ``answers``
    without repeating the effect.
    """

    request: Request
    answers: list[Answer]
    events: list[CoreEvent]
    sealed: bool = True
    delivered: bool = False


@dataclass
class Trace[S: StrategyState]:
    """Everything one drive saw, in order, and where it ended."""

    core: CoreState
    search: SearchDriver[S, Any]
    state: S
    decisions: list[Submission] = field(default_factory=list)
    events: list[StrategyEvent] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    # What core kept from the search: failures of its own requests and late results.
    discarded: list[Discarded] = field(default_factory=list)
    # Requests as dispatched and executor observations as delivered, interleaved in order.
    log: list[Request | RequestObserved] = field(default_factory=list)
    finished: bool = False
    steps: int = 0
    effects: list[str] = field(default_factory=list)
    """The id of every request whose effect happened, once per time it happened."""
    crashes: int = 0
    commits: int = 0
    """How many commit points the drive passed (the numbers ``Faults.crash_after`` names)."""

    @property
    def view(self) -> CoreView:
        """The run as core projects it now."""
        return project(self.core)


class _Driver[S: StrategyState]:
    def __init__(
        self,
        search: SearchDriver[S, Any],
        script: Script,
        harness: Harness,
        faults: Faults,
        core: CoreState,
    ) -> None:
        self.script = script
        self.registry = harness.registry
        self.step = partial(step, reducers=harness.reducers)
        self.host = SearchHost(step=self.step, registry=harness.registry)
        self.faults = faults
        self.search = search
        self.trace: Trace[S]
        self.core = core
        self.wakes: list[float] = []
        self.held: tuple[Decision, ...] = ()
        self.clock = 1.0
        self.calls: dict[str, int] = {}
        self.ledger: dict[str, _Executed] = {}
        self.commits = 0
        self.recovering = False
        self.ticked: CoreState | None = None
        self.in_turn = 0
        self.envelope_type = harness.envelope_type
        self.during_turn = harness.during_turn
        self.operator = harness.operator
        self.shell_events = harness.shell_events
        self.schemas = harness.schemas
        self.ended: dict[ResourceId, JobObserved] = {}
        """The last observation delivered of each job that has ended, by job."""
        self.reobserve_owed = 0
        self.replay_owed = 0

    # -- the shell loop ---------------------------------------------------

    def run(self, max_steps: int) -> None:
        while self.trace.steps < max_steps and not self.trace.finished:
            try:
                if self.recovering:
                    self.recover()
                progressed = self.dispatch_all() or self.control()
                if not progressed:
                    progressed = self.tick()
                if not progressed:
                    return
            except _CrashError:
                self.crash()

    def commit(self) -> None:
        """A commit point: the host dies here when the drive says so."""
        if self.in_turn:
            return  # the turn is running: a host that dies now leaves it begun, which is not modelled
        self.commits += 1
        self.trace.commits = self.commits
        if self.commits in self.faults.reobserve_after:
            self.reobserve_owed += 1
        if self.commits in self.faults.replay_after:
            self.replay_owed += 1
        if self.commits in self.faults.crash_after:
            raise _CrashError

    def crash(self) -> None:
        """The host died: results in flight are lost, and executors that had begun may not have sealed."""
        self.trace.crashes += 1
        for entry in self.ledger.values():
            if not entry.delivered and entry.sealed:
                entry.sealed = not self.faults.unsealed(entry.request)
                if not entry.sealed:
                    entry.events = []
        self.recovering = True

    def recover(self) -> None:
        """A new host: reload what was persisted and go on from it, as the step loop does."""
        self.reload()
        self.recovering = False
        self.commit()

    def start(self, config: object, events: tuple[CoreEvent, ...]) -> None:
        """Deliver the clock and the harness events, then begin the search's run."""
        published: list[StrategyEvent] = []
        incoming: list[Event] = []
        discarded: list[Discarded] = []
        core = self.core
        for event in (ClockAdvanced(now_at=1.0), *events):
            transition = self.step(core, event)
            core = transition.state
            published.extend(transition.events)
            translated = translate(core, transition.events)
            incoming.extend(translated.events)
            discarded.extend(translated.discarded)
        arrived = Inbox(tuple(incoming), (), tuple(discarded))
        advanced = begin(self.search, config, core, self.host, arrived)
        self.trace = Trace(core=core, search=self.search, state=advanced.state)
        self.trace.discarded.extend(advanced.discarded)
        self.trace.steps = 1 + len(events)
        self.take(
            advanced.state,
            advanced.core,
            (*published, *advanced.published),
            advanced.submitted,
        )
        self.wakes.extend(advanced.wakes)
        self.held = advanced.held

    def take(
        self,
        state: S,
        core: CoreState,
        published: tuple[StrategyEvent, ...],
        submitted: tuple[Submission, ...],
    ) -> None:
        trace = self.trace
        trace.state = state
        trace.core = core
        self.core = core
        trace.decisions.extend(submitted)
        trace.events.extend(published)
        trace.steps += 1 + len(submitted)
        if core.run.status.value == "terminal":
            trace.finished = True

    def control(self) -> bool:
        """Deliver what the operator does while nothing is dispatchable; whether it did anything."""
        events = self.operator(self.trace.core)
        for event in events:
            self.consume(event)
            self.commit()
        return bool(events)

    def tick(self) -> bool:
        """Move the clock to the next wake-up or request limit; deliver what that causes."""
        trace = self.trace
        due = [*self.wakes]
        asked = project(trace.core).next_observe_at
        if asked is not None:
            due.append(asked)
        limit = next_end(trace.core)
        if limit is not None:
            due.append(limit)
        if not due and trace.core == self.ticked:
            return False
        # The shell's loop delivers the clock after every pass that changed state, even with
        # nothing due: a closing run whose last cleanup was a late observation drains on that
        # tick (``scheduling._fill`` re-checks the drain there), not on the observation.
        moment = max(min(due, default=0.0), trace.core.run.now_at)
        before = trace.core
        reached = tuple(TimeReached(time=wake) for wake in sorted(self.wakes) if wake <= moment)
        self.wakes = [wake for wake in self.wakes if wake > moment]
        self.consume(ClockAdvanced(now_at=moment), extra=reached)
        self.end_at_limits()
        self.ticked = trace.core
        self.commit()
        return trace.core != before or bool(reached)

    def dispatch_all(self) -> bool:
        any_dispatched = False
        while True:
            batch = self.authorize_round()
            if not batch:
                return any_dispatched
            any_dispatched = True
            deliveries = [(request, self.observe(request)) for request in batch]
            if self.faults.reorder:
                deliveries.reverse()
            for request, delivery in deliveries:
                for event in delivery:
                    self.note_end(event)
                    for _ in range(2 if self.faults.duplicate else 1):
                        self.consume(event)
                executed = self.ledger.get(self.key_of(request))
                if executed is not None:
                    executed.delivered = True
                self.commit()
                self.reobserve()
                self.replay()

    # -- jobs observed again after their end ---------------------------------------------

    def note_end(self, event: CoreEvent) -> None:
        """Remember the first observation that reports a job's end."""
        if isinstance(event, JobObserved) and _ends(event.observation):
            self.ended.setdefault(event.resource_id, event)

    def again(self, resource: ResourceId) -> None:
        """The executor answers again for an ended job: the same end, at a newer sequence and time."""
        ended = self.ended[resource]
        key = ended.observation.request_id.root
        sequence = self.calls.get(key, 0)
        self.calls[key] = sequence + 1
        self.clock += 1.0
        view = ended.observation.model_copy(
            update={
                "event_id": EventId(root=f"{key}:observation:{sequence}"),
                "sequence": sequence,
                "observed_at": self.clock,
            }
        )
        event = JobObserved(
            resource_id=resource, observation=view, evaluation_result=ended.evaluation_result
        )
        self.ended[resource] = event
        self.consume(event)
        self.commit()

    def reobserve(self) -> None:
        """Answer again for every ended job once per ``reobserve_after`` point passed."""
        while self.reobserve_owed and not self.trace.finished:
            self.reobserve_owed -= 1
            for resource in tuple(self.ended):
                self.again(resource)

    def replay(self) -> None:
        """Deliver again every poll answer delivered so far, once per ``replay_after`` point."""
        while self.replay_owed and not self.trace.finished:
            self.replay_owed -= 1
            log = self.trace.log
            polls = {self.key_of(item) for item in log if isinstance(item, ObserveOwnedJob)}
            delivered = [
                item
                for item in log
                if isinstance(item, RequestObserved) and item.observation.request_id.root in polls
            ]
            for event in delivered:
                self.consume(event)
            self.commit()

    def authorize_round(self) -> list[Request]:
        """Authorize every prepared request core lets go now, in ledger order."""
        batch: list[Request] = []
        for prepared in self.trace.core.records.records:
            if prepared.phase is not RequestPhase.PREPARED:
                continue
            # An earlier authorization in this round can end a later request (a close settles the
            # inspection that waited on it): read it as the ledger holds it now.
            at = self.trace.core.records.positions(prepared.request_id)
            record = self.trace.core.records.records[at[0]] if at else None
            if record is None or record.phase is not RequestPhase.PREPARED:
                continue
            try:
                self.consume(DispatchAuthorized(request_id=record.request_id))
            except ContractError as error:
                if error.path in (("dependency",), ("recovery",)):
                    continue
                raise
            self.commit()
            batch.append(record.request)
        return batch

    # -- executors --------------------------------------------------------

    @staticmethod
    def key_of(request: Request) -> str:
        return request.request_id.root if request.request_id is not None else ""

    def observe(self, request: Request) -> list[CoreEvent]:
        key = self.key_of(request)
        self.trace.requests.append(request)
        self.trace.log.append(request)
        if isinstance(request, InspectRequest | InspectTurn):
            return self.inspect(request)
        held = self.ledger.get(key)
        if held is not None:  # sent again after a crash: the executor resumes, it does not repeat
            if not held.sealed:
                held.events = self.build_events(request, held.answers)
                held.sealed = True
            held.delivered = False
            return list(held.events)
        self.in_turn += 1
        for event in self.during_turn(request, self.trace.core):
            self.consume(event)
            self.dispatch_all()
        self.in_turn -= 1
        answer = self.script(request, self.trace.core)
        answers = list(answer) if isinstance(answer, tuple) else [answer]
        if self.faults.retry_first(request):
            answers.insert(0, Retryable())
        if self.faults.unknown_first(request):
            answers.insert(0, Unknown())
        if not isinstance(request, BlockRequest | CancelOwnedResource):
            self.trace.effects.append(key)
        events = self.build_events(request, answers)
        self.ledger[key] = _Executed(request, answers, events)
        return list(events)

    def build_events(self, request: Request, answers: list[Answer]) -> list[CoreEvent]:
        key = self.key_of(request)
        events: list[CoreEvent] = []
        for item in answers:
            sequence = self.calls.get(key, 0)
            self.calls[key] = sequence + 1
            if self.shell_events:
                events.extend(self.shell_observation(request, item, sequence))
            else:
                events.append(self.observation(request, item, sequence))
        return events

    # -- inspection after a crash ----------------------------------------------------

    def inspect(self, request: InspectRequest | InspectTurn) -> list[CoreEvent]:
        """Answer an inspection from what the executors did: sealed, begun, or never begun.

        A turn's session journal knows how the turn ended whether or not the host sealed its
        request, so an inspection of a turn that was dispatched always finds its reply.
        """
        turn = isinstance(request, InspectTurn)
        target = request.dispatch if isinstance(request, InspectTurn) else request.target
        if target is None:
            message = "a turn inspection names the request that dispatched the turn"
            raise ValueError(message)
        held = self.ledger.get(target.root)
        if turn and held is not None and not held.sealed:
            held.events = self.build_events(held.request, held.answers)
            held.sealed = True
        sequence = self.calls.get(self.key_of(request), 0)
        self.calls[self.key_of(request)] = sequence + 1
        query = self.observation(request, Succeeded(), sequence)
        owner: list[CoreEvent] = []
        if held is not None and held.sealed:
            seen = [event for event in held.events if isinstance(event, RequestObserved)]
            facts = {
                name: getattr(seen[-1], name)
                for name in RequestObserved.model_fields
                if name not in {"kind", "target"}
            }
            found = TargetObservation.model_validate(
                facts, context={"operation_registry": self.registry}
            )
            owner = [event for event in held.events if not isinstance(event, RequestObserved)]
        else:
            began = held is not None
            found = TargetObservation(
                observation=self.unproven(request, target, terminal=not began),
                reissue=ReissueProof.BEGUN_UNSEALED if began else ReissueProof.NEVER_BEGAN,
            )
        return [query.model_copy(update={"target": found}), *owner]

    def unproven(
        self, request: InspectRequest | InspectTurn, target: RequestId, *, terminal: bool
    ) -> Observation:
        """The target seen by an executor that has no sealed result for it."""
        number = self.calls.get(target.root, 0)
        self.calls[target.root] = number + 1
        self.clock += 1.0
        return Observation(
            event_id=EventId(root=f"{target.root}:observation:{number}"),
            request_id=target,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=number,
            observed_at=self.clock,
            status=ObservationStatus.REJECTED if terminal else ObservationStatus.UNKNOWN,
            accepted=False,
            terminal=terminal,
            # A request that never began holds nothing: nothing is left to release.
            released=terminal,
            children_complete=terminal,
            diagnostic="no sealed result for this request",
        )

    # -- the production executors' owner events -----------------------------------------

    def shell_observation(self, request: Request, answer: Answer, sequence: int) -> list[CoreEvent]:
        """The observation of ``request`` and the owner events its production executor attaches."""
        reply = self.parsed(request, answer)
        observed = self.observation(
            request, reply if isinstance(reply, _InvalidReply) else answer, sequence
        )
        match request:
            case DispatchTurn() | ResumeSessionTurn():
                return self.turn_events(request, observed, reply)
            case SubmitMeasurement():
                return self.submission_events(observed)
            case ObserveOwnedJob() | CollectEvidence():
                return self.job_events(request, answer, observed, sequence)
            case _:
                owner = self.owner_event(request, observed)
                return [observed] if owner is None else [observed, owner]

    @staticmethod
    def turn_events(
        request: DispatchTurn | ResumeSessionTurn,
        observed: RequestObserved,
        reply: Answer | _InvalidReply,
    ) -> list[CoreEvent]:
        """The turn's observation, and the session owner's event that carries its reply.

        The reply and the wait the agent validated travel on the owner's event, never on the
        request's own observation.
        """
        errors = reply.errors if isinstance(reply, _InvalidReply) else ()
        observed = observed.model_copy(
            update={
                "outcome_json": None,
                "outcome_schema": None,
                "suspension": None,
                "reply_errors": errors,
            }
        )
        text = reply.output_json if isinstance(reply, Succeeded) else None
        return [
            observed,
            TurnObserved(
                invocation=InvocationRef(
                    session_id=request.turn.session.session_id,
                    invocation_id=request.turn.invocation_id,
                    generation=request.scope.generation,
                ),
                observation=observed.observation,
                output_schema=None if text is None else request.turn.output_schema,
                output_json=text,
                reply_errors=errors,
                suspension=reply.suspension if isinstance(reply, Succeeded) else None,
            ),
        ]

    @staticmethod
    def submission_events(observed: RequestObserved) -> list[CoreEvent]:
        """A measurement submission, and the job it started when it was accepted."""
        view = observed.observation
        events: list[CoreEvent] = [
            observed,
            MeasurementSubmissionObserved(observation=view, failure=observed.measurement_failure),
        ]
        if view.accepted and view.resource_id is not None:
            events.append(
                JobObserved(
                    resource_id=view.resource_id,
                    observation=view,
                    evidence=observed.evidence,
                    evaluation_result=observed.evaluation_result,
                )
            )
        return events

    def job_events(
        self,
        request: ObserveOwnedJob | CollectEvidence,
        answer: Answer,
        observed: RequestObserved,
        sequence: int,
    ) -> list[CoreEvent]:
        """A poll or a collection, and the job state it found when the job was accepted."""
        if not (
            isinstance(answer, Succeeded | Running)
            or (isinstance(answer, Failed) and answer.accepted)
        ):
            return [observed]
        job = self.job_event(request, observed)
        if isinstance(answer, Running):
            # A poll is a query: it completes with the job's state, and the state
            # it found is the job's own observation.
            return [self.observation(request, Succeeded(), sequence), job]
        return [observed, job]

    @staticmethod
    def owner_event(request: Request, observed: RequestObserved) -> CoreEvent | None:
        """The event of the session, workspace or adoption owner that ``request`` causes."""
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

    def parsed(self, request: Request, answer: Answer) -> Answer | _InvalidReply:
        """A turn's reply is what its schema parses it to, as the session executor reports it."""
        if (
            not isinstance(request, DispatchTurn | ResumeSessionTurn)
            or not isinstance(answer, Succeeded)
            or answer.output_json is None
            or request.turn.output_schema not in self.schemas
        ):
            return answer
        try:
            checked = self.schemas[request.turn.output_schema].model_validate_json(
                answer.output_json
            )
        except ValidationError as error:
            return _InvalidReply(answer.resource_id, reply_errors(error))
        return replace(answer, output_json=checked.model_dump_json())

    def job_event(
        self, request: ObserveOwnedJob | CollectEvidence, observed: RequestObserved
    ) -> JobObserved:
        """The job's own observation, carrying its submission's identity as production does."""
        job = next(
            row
            for row in self.trace.core.evaluation.jobs
            if isinstance(row, OwnedJob) and row.resource_id == request.resource_id
        )
        key = job.submission_id.root
        sequence = self.calls.get(key, 0)
        self.calls[key] = sequence + 1
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

    def observation(
        self, request: Request, answer: Answer | _InvalidReply, sequence: int
    ) -> RequestObserved:
        self.clock += 1.0
        if request.request_id is None:
            message = "a dispatched request carries its identity"
            raise ValueError(message)
        status, accepted, terminal = _shape(answer)
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
            children_complete=terminal
            or (self.shell_events and accepted and isinstance(request, SubmitMeasurement)),
            resource_id=(
                answer.resource_id
                if isinstance(answer, Succeeded | Running | _InvalidReply)
                else None
            ),
            revision=answer.revision if isinstance(answer, Succeeded) else None,
            measured_input=answer.measured_input if isinstance(answer, Succeeded) else None,
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

    def consume(self, event: CoreEvent, *, extra: tuple[Event, ...] = ()) -> None:
        trace = self.trace
        if isinstance(event, RequestObserved):
            trace.log.append(event)
        self.take_transition(self.step(trace.core, event), extra)

    def end_at_limits(self) -> None:
        """What the runner loop records at the run clock: the end of every step past its limit."""
        while (ended := end_one(self.trace.core, self.trace.core.run.now_at)) is not None:
            state, heard = ended
            self.take_transition(Transition(state=state, requests=(), events=heard), ())
            self.commit()

    def take_transition(self, transition: Transition, extra: tuple[Event, ...]) -> None:
        trace = self.trace
        translated = translate(transition.state, transition.events)
        incoming = (*translated.events, *extra)
        advanced = advance(
            trace.search,
            trace.state,
            transition.state,
            self.host,
            Inbox(incoming, self.held, translated.discarded),
        )
        trace.discarded.extend(advanced.discarded)
        self.take(
            advanced.state,
            advanced.core,
            (*transition.events, *advanced.published),
            advanced.submitted,
        )
        self.wakes.extend(advanced.wakes)
        self.held = advanced.held
        if self.faults.reload:
            self.reload()

    def reload(self) -> None:
        trace = self.trace
        if self.envelope_type is None:
            message = "reloading needs Harness.envelope_type, the concrete RunEnvelope[State]"
            raise ValueError(message)
        envelope = self.envelope_type(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=HostFence(host_id=HostId(root="driver"), epoch=1),
            strategy_id=trace.core.run.declaration.strategy_id,
            state_schema=trace.core.run.declaration.state_schema,
            core=trace.core,
            strategy=trace.state,
            event_cursor=EventCursor(sequence=0),
        )
        loaded = self.registry.reload_envelope(self.envelope_type, envelope)
        trace.core = loaded.core
        trace.state = cast("S", loaded.strategy)


@dataclass(frozen=True)
class Harness:
    """What the host around the strategy provides: codec, run facts, capabilities, limits.

    ``events`` are delivered before the first decision (operator controls, for
    example). ``envelope_type`` is the concrete ``RunEnvelope[State]``, needed only
    for the reload fault. ``during_turn`` says which events a dispatched turn's own tool
    calls cause while it runs (an agent's evaluation request); they are delivered, and every
    request they prepare is dispatched, before the turn's observation. With ``shell_events``
    the driver attaches to each observation the owner events the production executors attach
    (a turn's reply, a session, workspace or adoption, a submission's job): without them a
    run cannot get past its first turn. ``schemas`` are the reply types of the turns, by the
    schema they are asked to answer; a reply that does not parse is reported as invalid.
    """

    registry: OperationRegistry
    facts: RunFacts
    lifecycle: frozenset[LifecycleCapability] = frozenset()
    limits: Limits = field(default_factory=sample_limits)
    events: tuple[CoreEvent, ...] = ()
    envelope_type: type[RunEnvelope] | None = None
    during_turn: Callable[[Request, CoreState], tuple[CoreEvent, ...]] = lambda _r, _c: ()
    operator: Callable[[CoreState], tuple[CoreEvent, ...]] = lambda _c: ()
    shell_events: bool = False
    schemas: Mapping[SchemaRef, type[BaseModel]] = field(default_factory=dict)
    deadline_at: float = 100000.0
    max_steps: int = 2000
    requirements: EvidenceRequirements = field(default_factory=EvidenceRequirements)
    reducers: CoreReducers | None = None
    """Sibling implementations core's step uses (see ``CoreReducers``); production when None."""


def new_run[S: StrategyState](search: SearchDriver[S, Any], harness: Harness) -> CoreState:
    """A run that has started: declaration validated against what the host offers."""
    base = initial_state()
    declaration = search.declaration
    registry = harness.registry
    offered = Capabilities(lifecycle=harness.lifecycle, operations=registry.descriptors)
    capabilities = validate_startup(declaration, offered)
    return base.model_copy(
        update={
            # The selection, as production stores it. The codec lists an optional
            # operation before an owned one; the declaration selects owned first.
            "registry": capabilities.operations,
            "run": base.run.model_copy(
                update={
                    "facts": harness.facts,
                    "declaration": declaration,
                    "capabilities": capabilities,
                    "limits": harness.limits,
                    "deadline_at": harness.deadline_at,
                    "requirements": harness.requirements,
                }
            ),
        }
    )


def drive[S: StrategyState, C](
    search: SearchDriver[S, C],
    config: C,
    script: Script,
    harness: Harness,
    faults: Faults | None = None,
) -> Trace[S]:
    """Run ``search`` on the real core until the run ends or nothing more happens.

    Core rejects an executor observation by raising, as the shell would halt; a
    rejected decision reaches the search as ``Failed(REFUSED)``. The trace holds
    every decision, request and published event, and the final core state.
    """
    core = new_run(search, harness)
    driver = _Driver(search, script, harness, faults or Faults(), core)
    driver.start(config, harness.events)
    driver.run(harness.max_steps)
    return driver.trace
