"""Authoritative outbox and class-driven operation observation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self, assert_never

if TYPE_CHECKING:
    from collections.abc import Mapping

from pydantic import BaseModel, Field, SerializeAsAny, ValidationInfo, model_validator

from vs_core._outcomes import OutcomeCodecError, OutcomeValue, bind_outcome

from .attempts import (
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    RestoreRevision,
    RetainRevision,
    SnapshotAndRetain,
    WorkspaceRequest,
)
from .common import (
    ContractValidationError,
    Count,
    ExecuteRegisteredOperation,
    InvocationId,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationId,
    OperationRef,
    OperationSchemaRef,
    RequestBase,
    RequestId,
    ResourceId,
    RevisionRef,
    SchemaRef,
    Scope,
    ScopeReopenNormalization,
    Seconds,
    SetupFailureKind,
    Value,
    unset,
    validate_setup_failure,
)
from .evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    Continuation,
    EvaluationRequest,
    EvidenceRef,
    InspectOwnedJob,
    MeasurementIdentity,
    ObserveOwnedJob,
    SubmitMeasurement,
)
from .evaluation_history import EvaluationTerminalFacts
from .job_observations import JobProgress, MeasurementFailure
from .sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    ResumeSessionTurn,
    SessionRequest,
    SnapshotAndRetainRun,
    TurnSpec,
)
from .settlement import AdoptionRequest, AdoptRevision, VerifyAdoption
from .until import Failed, FailureReason, FromDispatch, Inherited, ReplyError, Until, Watch, Within
from .waits import WaitOn


class InspectRequest(RequestBase):
    """Inspect request lifecycle contract."""

    kind: Literal["inspect_request"] = "inspect_request"
    target: RequestId
    resource_id: ResourceId | None = None


class CancelOwnedResource(RequestBase):
    """Cancel owned resource lifecycle contract."""

    kind: Literal["cancel_owned_resource"] = "cancel_owned_resource"
    resource_id: ResourceId
    target: RequestId


class BlockRequest(RequestBase):
    """Fence a record that reconciliation gave up on, for the typed reason it ended."""

    kind: Literal["block_request"] = "block_request"
    target: RequestId
    reason: FailureReason


type RegisteredOperationRequest = ExecuteRegisteredOperation
type ReconciliationRequest = Annotated[
    InspectRequest | CancelOwnedResource | BlockRequest, Field(discriminator="kind")
]
type Request = Annotated[
    WorkspaceRequest
    | SessionRequest
    | EvaluationRequest
    | AdoptionRequest
    | RegisteredOperationRequest
    | ReconciliationRequest,
    Field(discriminator="kind"),
]


class Attribution(StrEnum):
    """Where a request core creates takes its ``decision_id`` and ``admission_id`` from."""

    CAUSE = "cause"
    """The step that creates it: an unset field is filled from the decision and admission
    of the signal being handled, for a request that is that decision's own work."""
    OWN = "own"
    """The request itself, as the reducer built it; an unset ``decision_id`` stays unset.

    For a request whose id names what it belongs to (a job's poll or cancel, a submission
    core allocates for an agent's call or a base run): any signal in a step may ask for
    it, so filling it from the asker would give one id a payload per asker."""


class Outbound(Value):
    """A request together with how long it may take: the one way core creates a request.

    ``until`` is a ``Within`` (a duration from now, plus watches) or ``Inherited`` (the
    same ``Until`` as the decision that caused the request). Requests carry no deadline;
    core stores the resolved ``Until`` on the ``RequestRecord`` when it registers this.
    ``attribution`` says whether the step's cause may fill the request's decision.
    """

    request: Request
    until: Within | Inherited | Until | FromDispatch
    attribution: Attribution = Attribution.CAUSE


def within(request: Request, limit: float, *watches: Watch) -> Outbound:
    """``Outbound(request, Within(limit, watches))`` for core's own internal requests."""
    return Outbound(request=request, until=Within(limit=limit, watches=watches))


def inherit(request: Request) -> Outbound:
    """``Outbound`` that ends with the decision that caused ``request``."""
    return Outbound(request=request, until=Inherited())


class Scheduled(Value):
    """An ``Outbound`` whose ``until`` core has resolved to an absolute time."""

    request: Request
    until: Until
    limit_from_dispatch: Seconds | None = None
    """The seconds the work may take once dispatched, when ``until`` is only a placeholder."""


class CapacityHold(Value):
    """The time a turn spent held at the run's capacity gate, as the gate last reported it.

    The gate holds a turn its provider stopped (an outage, a spent quota) inside the turn's own
    call, so that time is the provider's, not the turn's work, and it is never charged to the
    turn's limit (``_turn_holds``). A report is cumulative: the latest replaces every earlier
    one. A hold runs from the turn's first capacity stop to the release after its last one, so
    the sends between its waits count too; only a send that ends the hold is the turn's.

    ``credit`` is the time of the turn's holds that ended; the gate bounds each of them (by its
    policy, or by the operator who resumed the run), so core credits it whole. ``since`` is
    when the hold the turn is in began (None when it is in none), and ``released_at`` when
    the gate last let the turn go: the end of the current hold's latest wait, or with no
    current hold the end of the hold that ended last (None while it waits, or if never held).
    A ``parked`` hold waits for the operator or a timed resume with the run paused, not for a
    backoff. ``budget`` is the most the current hold is credited: the outage budget the gate
    applies.

    ``resend_grace`` is the least time the send after a release has before core ends the
    turn, whatever own time the turn had left: the gate sizes it to one failing send, so a
    turn near its limit is not cut while its resend is still failing (an outage that started
    late in the turn), and the next stop reopens the hold before the grace runs out.
    """

    credit: Seconds = 0.0
    since: Seconds | None = None
    released_at: Seconds | None = None
    parked: bool = False
    budget: Seconds
    resend_grace: Seconds = 0.0

    @model_validator(mode="after")
    def consistent(self) -> Self:
        """A park belongs to a hold, and a parked hold is not released."""
        if self.since is None and self.parked:
            raise ContractValidationError("since", "a parked hold names its start")
        if (
            self.released_at is not None
            and self.since is not None
            and self.released_at < self.since
        ):
            raise ContractValidationError("released_at", "a hold is released after it began")
        if self.parked and self.released_at is not None:
            raise ContractValidationError("parked", "a parked hold waits; it is not released")
        return self

    @property
    def holding(self) -> bool:
        """Whether the gate holds the turn now: it waits, for a backoff or parked."""
        return self.since is not None and self.released_at is None


class TurnHeld(Value):
    """The run's capacity gate reports how long it has held the turn ``invocation_id``.

    The host's control bridge turns the gate's latest report into this event. Core keeps it on
    the turn's open dispatch record, whose limit it extends (``_turn_holds``); a report for a
    turn with no open dispatch record changes nothing.
    """

    kind: Literal["turn_held"] = "turn_held"
    invocation_id: InvocationId
    hold: CapacityHold


class RequestPhase(StrEnum):
    """RequestRecord phase lifecycle contract."""

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class RequestRecord(OutcomeValue):
    """Canonical outcome ledger including immutable typed setup classification.

    Records A retains root/target setup_failure with the corresponding observation
    and checks equality on replay before forwarding SessionObserved.failure.
    UNKNOWN never supplies nonacceptance, unsupported refund or success proof.
    evaluation_result retains the exact scientific ingress facts alongside its
    observation for same-sequence equality; Records A forwards them unchanged.
    """

    request_id: RequestId
    request: Request
    payload_digest: str
    lifecycle: LifecycleClass
    phase: RequestPhase
    sequence: Count | None = None
    observation: Observation | None = None
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN
    evaluation_result: EvaluationTerminalFacts | None = None
    suspension: Continuation | None = None
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    retry_count: Count = 0
    retry_at: Seconds | None = None
    """Core time at which the request goes out again after a retryable failure, else None."""
    until: Until
    """When core ends this record if it is still open, and the watches that end it sooner.

    A record that started a job has no limit while the job is queued, and each queued
    interval moves ``at`` later by its length, so its limit counts from the job's start
    (``_wait_clock``)."""
    failure: Failed | None = None
    """Why core ended it without a result (limit passed, watch fired), else None."""
    limit_from_dispatch: Seconds | None = None
    """The seconds the work may take from its dispatch, or None for a limit already absolute.

    Until the request is dispatched ``until.at`` is the run deadline (a queued request spends
    nothing); ``DispatchAuthorized`` then replaces ``until`` with the dispatch time plus this
    limit, once, and records ``dispatched_at``.
    """
    dispatched_at: Seconds | None = None
    """When the shell first dispatched a request whose limit counts from then, else None."""
    capacity_hold: CapacityHold | None = Field(default=None, exclude_if=unset)
    """For a turn's dispatch: the latest hold the run's capacity gate reported for the turn,
    which extends its limit (``_turn_holds``), else None."""
    dispatched_in: Count | None = None
    """The host epoch whose dispatch is the request's latest, else None (never dispatched).

    A request a dead host dispatched has no executor call of this epoch behind it: only an
    inspection or a re-send of this epoch can still answer it (``_waits._open``).
    """
    addressed_by: tuple[RequestId, ...]
    """The reconciliation commands (inspections, cancels, blocks) sent about this record.

    Each is added when it is registered, so a blocked record names what can still end it.
    """

    @property
    def retry_wait_until(self) -> float | None:
        """The core time at which a retryably failed request is sent again, else None."""
        if (
            self.phase == RequestPhase.DISPATCHED
            and self.observation is not None
            and self.observation.retryable
        ):
            return self.retry_at
        return None

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> RequestRecord:
        """Restore the owning subtype at the registered codec boundary."""
        if self.observation is None:
            if self.setup_failure != SetupFailureKind.UNKNOWN:
                raise ContractValidationError(
                    "setup_failure", "requires terminal setup observation"
                )
        else:
            validate_setup_failure(self.observation, self.setup_failure)
        if self.evaluation_result is not None:
            if self.observation is None:
                raise ContractValidationError(
                    "evaluation_result", "requires its accepted terminal observation"
                )
            self.evaluation_result.validate_observation(self.observation)
        return bind_outcome(self, info)


class ChildObservationWatermark(Value):
    """Highest accepted observation from one child-discovery source.

    The full immutable observation carries sequence and equality proof. Records
    B rejects an older sequence or conflicting facts at the same sequence before
    changing aggregate ownership, even when another source supplies that proof.
    """

    source_request: RequestId
    observation: Observation

    @model_validator(mode="after")
    def exact_source(self) -> ChildObservationWatermark:
        """A watermark belongs only to its observation's canonical source."""
        if self.source_request != self.observation.request_id:
            raise ContractValidationError("source_request", "differs from observation request")
        return self


class ChildLease(Value):
    """Discovered descendant ownership, distinct from its ancestor's record.

    Source requests and parent resources are unique, canonically ordered proofs.
    Conflicting scope or ancestry rejects; transfer to a typed owner is atomic.
    Records B maintains watermarks independently of aggregate observation.
    Incomplete historical watermarks cannot authorize release or transfer;
    reconcile every source before marking the history complete.
    Retained source claims conservatively retain independent ownership: run
    finality requires conclusive release from every source and preserves every
    retained source's descendant manifest. The aggregate observation cannot
    erase those claims. No alternative-reporter authority is offered.
    """

    resource_id: ResourceId
    scope: Scope
    source_requests: tuple[RequestId, ...] = Field(min_length=1)
    parent_resources: tuple[ResourceId, ...] = ()
    observation: Observation | None = None
    observation_watermarks: tuple[ChildObservationWatermark, ...] = ()
    watermark_history_complete: bool = False

    @model_validator(mode="after")
    def ownership_proofs(self) -> ChildLease:
        """Reject duplicate or noncanonical proof and resource correspondence."""
        for name, values in (
            ("source_requests", self.source_requests),
            ("parent_resources", self.parent_resources),
        ):
            roots = tuple(value.root for value in values)
            if roots != tuple(sorted(set(roots))):
                raise ContractValidationError(
                    name, "unique canonically ordered identities required"
                )
        if self.observation is not None and (
            self.observation.scope != self.scope or self.observation.resource_id != self.resource_id
        ):
            raise ContractValidationError(
                "observation", "child scope and resource_id must match its lease"
            )
        self._validate_watermarks()
        return self

    def _validate_watermarks(self) -> None:
        """Incomplete migrated history grants no stale-source release authority."""
        sources = tuple(mark.source_request.root for mark in self.observation_watermarks)
        if sources != tuple(sorted(set(sources))):
            raise ContractValidationError(
                "observation_watermarks", "unique canonically ordered sources required"
            )
        for mark in self.observation_watermarks:
            if (
                mark.source_request not in self.source_requests
                or mark.observation.scope != self.scope
                or mark.observation.resource_id != self.resource_id
            ):
                raise ContractValidationError(
                    "observation_watermarks", "source, scope and child resource must match lease"
                )
        if self.watermark_history_complete:
            if set(sources) != {source.root for source in self.source_requests}:
                raise ContractValidationError(
                    "watermark_history_complete", "every source requires its watermark"
                )
            if self.observation is not None and not any(
                mark.observation == self.observation for mark in self.observation_watermarks
            ):
                raise ContractValidationError(
                    "observation", "aggregate proof must match an accepted source watermark"
                )


class RecoveryPhase(StrEnum):
    """Recovery gates admission independently of the run's pause state."""

    REQUIRED = "required"
    RECOVERING = "recovering"
    READY = "ready"
    BLOCKED = "blocked"


class RecoveryCheck(Value):
    """Original target resolved by inspection, never command success alone."""

    target: RequestId
    inspection: RequestId | None = None
    resolution: Literal[
        "pending", "safe-prepared", "reattached", "terminal", "unknown", "blocked"
    ] = "pending"
    """How the check resolved. ``unknown``: its target's result is discarded whatever it was (a
    cancel of a job) and its executor could not say, so recovery settled it without an answer."""
    wait: WaitOn | None
    """What a pending check waits on: its target while that is open, the inspections recovery
    sent about the target and the children it left, and the owned jobs that settle children
    their job ledger tracks. Only a pending check waits."""

    @model_validator(mode="after")
    def waits_only_while_pending(self) -> RecoveryCheck:
        """A resolved or blocked check waits on nothing."""
        if self.wait is not None and self.resolution != "pending":
            raise ContractValidationError("wait", f"a {self.resolution} recovery check waits")
        return self


class RecoveryBarrier(Value):
    """Fence-epoch barrier persisted before ordinary admission or dispatch.

    READY requires every target safe to dispatch, positively reattached, or
    conclusively terminal. BLOCKED still permits bounded inspection and cleanup.
    Live jobs may remain owned when recovery finishes. Stale epochs cannot
    resolve checks from a newer startup.
    """

    epoch: Count = 0
    phase: RecoveryPhase = RecoveryPhase.REQUIRED
    checks: tuple[RecoveryCheck, ...] = ()

    @model_validator(mode="after")
    def ready_proofs(self) -> RecoveryBarrier:
        """No original target is checked twice or admitted without resolution."""
        targets = tuple(check.target for check in self.checks)
        if len(set(targets)) != len(targets):
            raise ContractValidationError("checks", "duplicate recovery target")
        if self.phase == RecoveryPhase.READY and any(
            check.resolution in ("pending", "blocked") for check in self.checks
        ):
            raise ContractValidationError(
                "checks",
                "READY requires safe-prepared, reattached, terminal or unknown-settled checks",
            )
        return self


HOST_LOG_LENGTH = 2
"""Host epochs core keeps: the current one and the one before it, which bound the last downtime."""


class HostEpoch(Value):
    """One lease acquisition (an epoch): when its host started and when it last proved itself alive.

    ``alive_at`` is the host's last recorded lease renewal while the epoch is current. When the
    next epoch opens it becomes the later of that and the host's last commit, the last moment
    the host is known to have been alive.
    """

    epoch: Count
    started_at: Seconds
    alive_at: Seconds

    @model_validator(mode="after")
    def alive_after_start(self) -> HostEpoch:
        """A host proves itself alive no earlier than it started."""
        if self.alive_at < self.started_at:
            raise ContractValidationError("alive_at", "a host is alive no earlier than it started")
        return self


_LEDGER_INDEXES = ("_by_resource", "_inspections", "_positions")
"""The slots of `RecordsState` that hold its ledger indexes.

Named here, once, so that `RecordsState.model_copy` reads this tuple instead of the class's
``__slots__``, a dunder the purity ratchet keeps core from reading.
"""


class RecordsState(Value):
    """Records state lifecycle contract."""

    # The indexes from request identity, from the resource a request names, and from the request
    # an inspection inspects, to positions in ``records``, each built on its first lookup. A
    # state never changes, so neither do its indexes. Like `Value`'s slots they stay out of
    # equality and serialization; a copy that keeps the same ``records`` tuple keeps them too
    # (see `model_copy`).
    __slots__ = _LEDGER_INDEXES

    records: tuple[RequestRecord, ...] = ()
    children: tuple[ChildLease, ...] = ()
    recovery: RecoveryBarrier = RecoveryBarrier()
    hosts: tuple[HostEpoch, ...] = Field(max_length=HOST_LOG_LENGTH)
    """The latest host epochs, oldest first; the last is the recovery barrier's epoch."""

    @model_validator(mode="after")
    def host_log(self) -> RecordsState:
        """Epochs strictly increase and the newest is the one recovery started."""
        epochs = [host.epoch for host in self.hosts]
        if epochs != sorted(set(epochs)):
            raise ContractValidationError("hosts", "host epochs must strictly increase")
        if epochs and epochs[-1] != self.recovery.epoch:
            raise ContractValidationError("hosts", "the newest host epoch is the recovery epoch")
        return self

    def model_copy(self, *, update: Mapping[str, object] | None = None, deep: bool = False) -> Self:
        """A copy that keeps the ledger indexes when it keeps the very same ``records`` tuple.

        The indexes are positions in ``records`` and nothing else, so they hold for any copy
        whose ``records`` is the same object. Without this, each copy that changed only
        ``children`` or ``recovery`` rebuilt the index on its next lookup: recovery copies the
        state once per finished record, so a restart cost the square of the ledger.
        """
        copy = super().model_copy(update=update, deep=deep)
        if copy.records is self.records:
            for slot in _LEDGER_INDEXES:
                try:
                    object.__setattr__(copy, slot, object.__getattribute__(self, slot))
                except AttributeError:
                    continue
        return copy

    def positions(self, request_id: RequestId | None) -> tuple[int, ...]:
        """Where the records of ``request_id`` sit in ``records``: one place, or none.

        More than one is a corrupt ledger, which the callers that need a unique record reject.
        A lookup costs a dictionary read instead of comparing the identity with every record.
        """
        try:
            index: dict[RequestId, tuple[int, ...]] = object.__getattribute__(self, "_positions")
        except AttributeError:
            index = {}
            for position, record in enumerate(self.records):
                index[record.request_id] = (*index.get(record.request_id, ()), position)
            object.__setattr__(self, "_positions", index)
        return () if request_id is None else index.get(request_id, ())

    def about_resource(self, resource_id: ResourceId) -> tuple[RequestRecord, ...]:
        """The records whose request names ``resource_id`` (a poll, inspection or cancel of it).

        In ledger order. A lookup costs a dictionary read instead of a scan of every record.
        """
        try:
            index: dict[ResourceId, tuple[int, ...]] = object.__getattribute__(self, "_by_resource")
        except AttributeError:
            index = {}
            for position, record in enumerate(self.records):
                named = getattr(record.request, "resource_id", None)
                if named is not None:
                    index[named] = (*index.get(named, ()), position)
            object.__setattr__(self, "_by_resource", index)
        return tuple(self.records[position] for position in index.get(resource_id, ()))

    def inspections_of(self, target: RequestId) -> tuple[RequestRecord, ...]:
        """The records of the inspections of ``target``, in ledger order.

        A lookup costs a dictionary read instead of a scan of every record.
        """
        try:
            index: dict[RequestId, tuple[int, ...]] = object.__getattribute__(self, "_inspections")
        except AttributeError:
            index = {}
            for position, record in enumerate(self.records):
                if isinstance(record.request, InspectRequest):
                    named = record.request.target
                    index[named] = (*index.get(named, ()), position)
            object.__setattr__(self, "_inspections", index)
        return tuple(self.records[position] for position in index.get(target, ()))

    def find(self, request_id: RequestId | None) -> tuple[RequestRecord, ...]:
        """The records of ``request_id``, in ledger order (see `positions`)."""
        return tuple(self.records[position] for position in self.positions(request_id))

    def record(self, request_id: RequestId | None) -> RequestRecord | None:
        """The first record of ``request_id``, as a scan of ``records`` would find it."""
        positions = self.positions(request_id)
        return self.records[positions[0]] if positions else None


class OperationView(Value):
    """Operation view lifecycle contract."""

    operation_id: OperationId
    scope: Scope
    schema_ref: OperationSchemaRef
    phase: RequestPhase
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)


class RequestPrepared(Value):
    """Canonical proposal before an executable record acquires final payload.

    For registered session turns, Records A delegates acquisition and input
    reservation first. Sessions A validates the accepted Operation receipt and
    normalized turn, then returns the same ExecuteRegisteredOperation identity
    with its fixed occurrence manifest. Only that final payload is registered
    and persisted before dispatch. A proposal with empty inputs is never proof
    that reservation completed or authority to dispatch a later changed payload.
    normalized_measurement is the owning registration's canonical expectation;
    absence never derives measurement identity from observed evidence.
    """

    kind: Literal["request_prepared"] = "request_prepared"
    request: Request
    until: Until
    lifecycle: LifecycleClass
    normalized_turn: TurnSpec | None = None
    normalized_measurement: MeasurementIdentity | None = None
    normalized_scope_reopen: ScopeReopenNormalization | None = None


class DispatchAuthorized(Value):
    """Dispatch authorized lifecycle contract."""

    kind: Literal["dispatch_authorized"] = "dispatch_authorized"
    request_id: RequestId
    now_at: Seconds | None = None
    """The shell's time at the dispatch, which starts the limit of a ``FromDispatch`` request.

    None leaves core's time as it is.
    """


class ReissueProof(StrEnum):
    """What an executor's own record proves about an inspected request that has no result."""

    NEVER_BEGAN = "never_began"
    """No begun record: the effect never started, so the same request is simply sent again."""

    BEGUN_UNSEALED = "begun_unsealed"
    """A begun record and no sealed result: the effect may have run, so the request is sent
    again to resume, and its executor inspects the external effect before repeating it."""


class TargetObservation(OutcomeValue):
    """Lifecycle facts for the inspected target, separate from query completion.

    Its request ID names the original target. A child result names the exact
    child resource and cannot overwrite a parent observation or registered
    outcome. Root outcomes bind to the target descriptor. Query and target
    sequences are independent; command and target facts commit atomically.
    """

    observation: Observation
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN
    target_resource: ResourceId | None = None
    revision: RevisionRef | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    progress: JobProgress | None = None
    measurement_failure: MeasurementFailure | None = None
    evaluation_result: EvaluationTerminalFacts | None = None
    suspension: Continuation | None = None
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    reply_errors: tuple[ReplyError, ...] = ()
    """What a rejected turn reply got wrong, field by field, for the correction that follows."""
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    operation_schema: OperationSchemaRef | None = None
    reissue: ReissueProof | None = None
    """Why core may send the inspected request out again, when the executor can prove it."""

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> TargetObservation:
        """Restore registered target subtypes at the owning codec boundary."""
        validate_setup_failure(self.observation, self.setup_failure)
        self._validate_reissue()
        if self.evaluation_result is not None:
            self.evaluation_result.validate_observation(self.observation)
        if self.target_resource is not None and (
            self.observation.resource_id != self.target_resource
        ):
            raise ContractValidationError(
                "target_resource", "must match the inspected child observation"
            )
        if self.target_resource is not None and any(
            value is not None
            for value in (
                self.operation_schema,
                self.outcome_schema,
                self.outcome_json,
                self.outcome,
            )
        ):
            raise ContractValidationError(
                "target_resource", "child lease cannot carry a root registered outcome"
            )
        if self.progress is not None and (
            self.progress.observation_sequence != self.observation.sequence
            or self.progress.observed_at != self.observation.observed_at
        ):
            raise ContractValidationError(
                "progress", "sequence and time must match its observation"
            )
        return bind_outcome(self, info)

    def _validate_reissue(self) -> None:
        observation = self.observation
        owns_nothing = (
            observation.resource_id is None
            and not observation.children
            and self.target_resource is None
        )
        match self.reissue:
            case None:
                return
            case ReissueProof.NEVER_BEGAN:
                valid = (
                    observation.status == ObservationStatus.REJECTED
                    and observation.terminal
                    and not observation.accepted
                    and owns_nothing
                )
            case ReissueProof.BEGUN_UNSEALED:
                valid = observation.status == ObservationStatus.UNKNOWN and not observation.terminal
            case _ as unreachable:
                assert_never(unreachable)
        if not valid:
            raise ContractValidationError(
                "reissue", f"{self.reissue.value} does not fit the observation it accompanies"
            )


class RequestObserved(OutcomeValue):
    """Root request outcome with executor-owned typed setup failure.

    Inspection's setup classification belongs in target.setup_failure, separately
    from the query outcome. Records A forwards the exact corresponding value to
    Sessions A and preserves it in the canonical ledger for equality on replay.
    """

    kind: Literal["request_observed"] = "request_observed"
    target: TargetObservation | None = None
    progress: JobProgress | None = None
    measurement_failure: MeasurementFailure | None = None
    evaluation_result: EvaluationTerminalFacts | None = None
    suspension: Continuation | None = None
    observation: Observation
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    reply_errors: tuple[ReplyError, ...] = ()
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    evidence: tuple[EvidenceRef, ...] = ()
    revision: RevisionRef | None = None
    operation_schema: OperationSchemaRef | None = None

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> RequestObserved:
        """Restore the owning subtype at the registered codec boundary."""
        validate_setup_failure(self.observation, self.setup_failure)
        if self.evaluation_result is not None:
            self.evaluation_result.validate_observation(self.observation)
        return bind_outcome(self, info)


class RecoveryStarted(Value):
    """A host acquired the lease: epoch ``epoch`` starts at ``now_at``."""

    kind: Literal["recovery_started"] = "recovery_started"
    epoch: Count
    now_at: Seconds


class HostRenewed(Value):
    """The current epoch's host renewed its lease at ``now_at``, so it was alive then."""

    kind: Literal["host_renewed"] = "host_renewed"
    epoch: Count
    now_at: Seconds


class RecoveryReady(Value):
    """Current-epoch recovery completion wakes scheduling without unpausing."""

    kind: Literal["recovery_ready"] = "recovery_ready"
    epoch: Count


class ReconciliationDeadline(Value):
    """Reconciliation deadline lifecycle contract."""

    kind: Literal["reconciliation_deadline"] = "reconciliation_deadline"
    request_id: RequestId
    now_at: Seconds
    reason: FailureReason = FailureReason.TIMED_OUT
    """TIMED_OUT when the record's limit passed, else the reason of the watch that fired."""


class RetryDue(Value):
    """Internal signal: core time reached the backoff of a request the executor failed retryably.

    Raised on every clock tick for each record whose ``retry_at`` has come; the ledger sends the
    request out again, unchanged. Never shell input.
    """

    kind: Literal["retry_due"] = "retry_due"
    request_id: RequestId
    now_at: Seconds


class RequestUndispatchable(Value):
    """Internal signal: a prepared request that nothing can ever authorize for dispatch.

    Raised by the step when it reaches quiescence with a prepared request whose dependencies
    have failed for good. The ledger closes it as cancelled-unsent, as it does for a dependent
    whose dependency failed first, so its owner hears a conclusive failure. Never shell input.
    """

    kind: Literal["request_undispatchable"] = "request_undispatchable"
    request_id: RequestId


class ReconciliationCommandLapsed(Value):
    """Internal signal: an inspection, cancel or block is still open at its limit.

    Raised on every clock tick for each such command. Its executor never answered, or answered
    without a conclusion (Unknown, or a failure it may retry), and nothing sends it again. The
    ledger blocks it, so no wait counts it as live any longer: the target's own deadline, the
    recovery check that sent it, or a closing run's stalled-record ending decides the target.
    Never shell input.
    """

    kind: Literal["reconciliation_command_lapsed"] = "reconciliation_command_lapsed"
    request_id: RequestId
    now_at: Seconds


class BlockedRecordStalled(Value):
    """Internal signal: a closing run holds a blocked record that nothing can still end.

    Raised by the step when it reaches quiescence with the run closing and a ``BLOCKED`` record
    that no live cancellation or inspection addresses: the bound already passed, the strategy
    was told (``RequestBlocked``), and only the close is waiting on the record. The ledger ends
    it with a conclusive failure that keeps every ownership fact the executor reported (a
    resource it named or children it found are not released by this signal). Never shell input.
    """

    kind: Literal["blocked_record_stalled"] = "blocked_record_stalled"
    request_id: RequestId


class RecoveryCheckWaitLapsed(Value):
    """Internal signal: nothing a pending recovery check names can still move it.

    Raised by the step at quiescence while recovery waits on the check's target, when every
    request the check names has ended and every entity it names stopped waiting. What the check
    waited on may have settled its target meanwhile (a session it waited on became known), so
    recovery resolves the check again from the current state and names what it waits on now.
    Never shell input.
    """

    kind: Literal["recovery_check_wait_lapsed"] = "recovery_check_wait_lapsed"
    target: RequestId


class OperationRetireRequested(Value):
    """Explicit retirement of a registered owned operation, resolved by records."""

    kind: Literal["operation_retire_requested"] = "operation_retire_requested"
    operation: OperationRef
    scope: Scope


class OperationResult(OutcomeValue):
    """Operation result lifecycle contract."""

    kind: Literal["operation_result"] = "operation_result"
    operation_id: OperationId
    observation: Observation
    outcome_schema: SchemaRef
    operation_schema: OperationSchemaRef
    outcome_json: str | None = None
    # The wire omits this derived field. The after-validator requires and restores
    # the registered subtype before a callback can escape the codec.
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> OperationResult:
        """Give strategy callbacks the owner's validated model, never an opaque dict."""
        validated = bind_outcome(self, info)
        if validated.outcome is None:
            raise OutcomeCodecError("context")
        return validated


type RecordsEvent = Annotated[
    RequestPrepared
    | DispatchAuthorized
    | RequestObserved
    | RecoveryStarted
    | RecoveryReady
    | ReconciliationDeadline
    | RetryDue
    | RequestUndispatchable
    | BlockedRecordStalled
    | ReconciliationCommandLapsed
    | RecoveryCheckWaitLapsed
    | OperationRetireRequested
    | HostRenewed
    | TurnHeld,
    Field(discriminator="kind"),
]


def success_without_reply(observation: Observation) -> bool:
    """Whether a turn observation proves a success whose reply only its owner event carries."""
    return (
        observation.terminal
        and observation.accepted
        and observation.status == ObservationStatus.SUCCEEDED
    )


def reply_owed(
    observation: Observation, facts: RequestObserved | TargetObservation | RequestRecord
) -> bool:
    """Whether a plain turn's observation proves a success whose reply only its owner event carries.

    *facts* is the answer or the record that stored it: both hold the same suspension and output.
    """
    return (
        success_without_reply(observation)
        and facts.suspension is None
        and facts.outcome_json is None
    )


def request_lifecycle(request: Request) -> LifecycleClass:
    """Classify the canonical request without reducer or backend knowledge."""
    match request:
        case ExecuteRegisteredOperation():
            lifecycle = request.operation.schema_ref.lifecycle
        case DispatchTurn() | ResumeSessionTurn():
            lifecycle = LifecycleClass.SESSION_TURN
        case SubmitMeasurement():
            lifecycle = LifecycleClass.OWNED_JOB
        case (
            InspectTurn()
            | ObserveOwnedJob()
            | InspectOwnedJob()
            | CollectEvidence()
            | InspectRequest()
            | VerifyAdoption()
        ):
            lifecycle = LifecycleClass.QUERY
        case (
            EnsureWorkspace()
            | RestoreRevision()
            | SnapshotAndRetain()
            | SnapshotAndRetainRun()
            | RetainRevision()
            | DiscardWorkspace()
            | CloseAttemptScope()
            | EnsureSession()
            | CancelTurn()
            | CloseSession()
            | CancelOwnedJob()
            | AdoptRevision()
            | CancelOwnedResource()
            | BlockRequest()
        ):
            lifecycle = LifecycleClass.IDEMPOTENT_WRITE
        case _:
            assert_never(request)
    return lifecycle
