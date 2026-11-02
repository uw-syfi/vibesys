"""Strict immutable values shared by lifecycle areas."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
    model_validator,
)

from vs_core._corevalue import ContractValidationError
from vs_core._generated.vibesys.core.v1.common_cv import (
    IDENTITY_PATTERN,
    ArtifactId,
    ArtifactRef,
    AttemptId,
    ChargeId,
    ContinuationId,
    ControlAction,
    ControlId,
    DecisionId,
    EventId,
    EvidenceId,
    HostId,
    Identity,
    InputId,
    InputMode,
    InvocationId,
    InvocationRef,
    ItemId,
    OperationId,
    PauseOwner,
    PoolId,
    RequestId,
    ResourceId,
    ResumeWhen,
    RevisionId,
    RoleId,
    RunId,
    RunStatus,
    SchemaRef,
    Scope,
    SessionId,
    SettlementId,
    StopMode,
    StrategyId,
)
from vs_core._generated.vibesys.core.v1.contracts_cv import PureOption
from vs_core._generated.vibesys.core.v1.control_cv import (
    MAX_REQUEST_RETRIES,
    ControlInput,
    InputTarget,
    InvocationInputTarget,
    ItemInputTarget,
    Limits,
    PoolCapacity,
    RequestRetries,
    RoleInputTarget,
    RunPark,
    RunPause,
    ScopeInputTarget,
)
from vs_core._generated.vibesys.core.v1.leaves_cv import (
    EvidenceKey,
    RevisionRef,
    TaskObjective,
    TaskStage,
)
from vs_core._generated.vibesys.core.v1.vocabulary_cv import (
    AssessmentKind,
    ChargeKind,
    DigestScheme,
    EvidenceKind,
    LifecycleClass,
    ObservationStatus,
)
from vs_core._values import deeply_immutable, emit_fragment

if TYPE_CHECKING:
    from pydantic import SerializationInfo


__all__ = [
    "IDENTITY_PATTERN",
    "MAX_CORRECTIONS",
    "MAX_LOST_TURNS",
    "MAX_REASK_CHAIN",
    "MAX_REQUEST_RETRIES",
    "MAX_SUBMISSIONS_PER_MEASUREMENT",
    "RETRYABLE_FAILURES",
    "THIS_RUN",
    "Area",
    "ArtifactId",
    "ArtifactRef",
    "AssessmentKind",
    "AttemptId",
    "AttemptRef",
    "Capabilities",
    "ChargeId",
    "ChargeKind",
    "ChargeReceipt",
    "CheckpointDecline",
    "ChildManifest",
    "CompletionStatus",
    "ContinuationId",
    "ContractValidationError",
    "ControlAction",
    "ControlId",
    "ControlInput",
    "Count",
    "DecisionId",
    "DependencyRef",
    "DependencyStatus",
    "DigestScheme",
    "EventCursor",
    "EventId",
    "EvidenceId",
    "EvidenceKey",
    "EvidenceKind",
    "ExecuteRegisteredOperation",
    "Generation",
    "HostFence",
    "HostId",
    "Identity",
    "IdentityTypeError",
    "InputId",
    "InputMode",
    "InputTarget",
    "InvocationId",
    "InvocationInputTarget",
    "InvocationRef",
    "ItemId",
    "ItemInputTarget",
    "KernelNotImplementedError",
    "LifecycleCapability",
    "LifecycleClass",
    "Limits",
    "Observation",
    "ObservationStatus",
    "OperationDescriptor",
    "OperationDescriptorError",
    "OperationId",
    "OperationNormalizationKind",
    "OperationRef",
    "OperationSchemaRef",
    "OperationWire",
    "PauseOwner",
    "PoolCapacity",
    "PoolId",
    "PureOption",
    "RejectionCode",
    "ReleaseDependency",
    "RequestBase",
    "RequestId",
    "RequestRetries",
    "ReservedInputOccurrence",
    "ResourceId",
    "ResumeWhen",
    "RevisionAuthority",
    "RevisionId",
    "RevisionNumber",
    "RevisionRef",
    "RoleId",
    "RoleInputTarget",
    "RunFacts",
    "RunId",
    "RunPark",
    "RunPause",
    "RunStatus",
    "SchemaRef",
    "Scope",
    "ScopeInputTarget",
    "ScopeReopenNormalization",
    "Seconds",
    "SessionId",
    "SettlementId",
    "SetupFailureKind",
    "SignalCycleError",
    "StopMode",
    "StrategyId",
    "TaskObjective",
    "TaskStage",
    "TaskTerms",
    "TurnFailureKind",
    "Value",
    "WorkspaceMode",
    "WorkspaceRef",
    "escalate_stop",
    "unset",
    "validate_setup_failure",
]


class CheckpointDecline(StrEnum):
    """Why an invocation checkpoint was not taken, so a decline is never silent."""

    WRITER_ACTIVE = "writer_active"
    """Another writer of the attempt's workspace has not ended, so a snapshot would
    be torn. The later writer's own terminal turn requests the checkpoint; when that turn
    ends without earning one (it failed or was cancelled), core asks again for the last
    declined one, and a ``Retained`` then follows the ``Declined``."""

    UNAUTHORIZED = "unauthorized"
    """Neither an interruption claim nor a terminal write turn authorizes it."""

    UNCHARGED = "uncharged"
    """The invocation has no live charge in this attempt, so it is not this
    attempt's work."""

    NOT_ACTIVE = "not_active"
    """The attempt is not active; its closure retains the workspace itself."""

    SNAPSHOT_FAILED = "snapshot_failed"
    """The executor ended the snapshot request without a retained revision. Closure
    still retains the workspace, so the attempt can release."""


class TurnFailureKind(StrEnum):
    """Why a turn ended without a usable reply, as a closed set strategies match on."""

    PROVIDER_FAILED = "provider_failed"
    """The executor ended the turn: the provider refused it, or its reply broke the schema."""
    TRANSPORT_LOST = "transport_lost"
    """Acceptance stayed unknown after inspection, so core released the turn unfinished."""
    CANCELLED = "cancelled"
    """Core cancelled the turn (stop, interruption or cleanup), so no reply exists."""
    EPOCH_INTERRUPTED = "epoch_interrupted"
    """The previous epoch exited before this invocation had a conclusive result."""
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """Core ended the turn while the run's capacity gate held it for its provider (an outage,
    a spent quota): no reply exists, and the turn's own work never failed. A strategy asks it
    again without counting it against the role's lost turns."""


def unset(value: object) -> bool:
    """Whether an optional field is unset (None) or a flag is off, so serialization leaves it out.

    For ``Field(exclude_if=unset)`` on optional fields and flags: left out when unset, a value that sets
    none of them serializes, and so digests, budget keys and request ids, exactly as before the
    field existed. That keeps identities stable. Any new field changes the envelope's shape and
    version, which needs its migration (``vs_core._envelope_migrations``).
    """
    return value is None or value is False or (isinstance(value, tuple) and not value)


class Value(BaseModel):
    """Persistable data, never live interfaces or mutable containers."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    # Pydantic hashes a frozen model by hashing every field, recursively, on every call. Core
    # keys its memoized checks (`lru_cache` on whole envelopes and requests) by these values, so
    # each lookup re-walked the entire unchanged history: the dominant cost of a step. A value
    # cannot change, so its hash is computed once. A slot keeps it out of `__dict__` and out of
    # equality, copies and serialization; a copy is a new object and hashes afresh. The other
    # slots hold facts derived from the fields (immutability verdict, canonical text, JSON
    # fragment; see `vs_core._values.recall`) under the same rule, and are why those facts cost
    # an attribute read instead of a side-table lookup.
    __slots__ = ("_canonical", "_hash", "_immutable", "_text")

    def __hash__(self) -> int:
        """Hash of the class's fields, computed once."""
        try:
            return self._hash
        except AttributeError:
            fields = self.__dict__
            # `model_construct` may leave fields unset; pydantic's own hash tolerates that. The
            # tuple's hash is the very value pydantic's generated `__hash__` returns; the
            # `hash` builtin is avoided only because the purity ratchet bars it from core.
            value = tuple.__hash__(
                tuple(fields.get(name) for name in type(self).__pydantic_fields__)
            )
            object.__setattr__(self, "_hash", value)
            return value

    def __deepcopy__(self, memo: dict[int, object] | None = None) -> Self:
        """The value itself when nothing in it can change, as for a tuple of strings."""
        if deeply_immutable(self):
            return self
        return super().__deepcopy__(memo)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo) -> object:
        # Writes nothing of its own: `encode_json` asks each frozen value for its remembered text.
        return emit_fragment(self, handler, info.context)


type Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Count = Annotated[int, Field(ge=0)]
type Generation = Annotated[int, Field(ge=0)]
type RevisionNumber = Annotated[int, Field(ge=0)]
type LifecycleCapability = Literal["park", "interrupt", "steer", "suspend", "profile-capture"]
"""Host-offered lifecycle abilities. "profile-capture" admits profile measurements."""


THIS_RUN = Scope(owner=RunId(root="this-run"), generation=0)
"""The scope a search names for work the run itself owns. Core replaces it with the run's own."""


class AttemptRef(Value):
    """Attempt ref lifecycle contract."""

    attempt_id: AttemptId
    generation: Generation


class OperationRef(Value):
    """Operation ref lifecycle contract."""

    operation_id: OperationId
    generation: Generation


class DependencyRef(Value):
    """Dependency ref lifecycle contract."""

    decision_id: DecisionId | None = None
    request_id: RequestId | None = None


RETRYABLE_FAILURES = frozenset(
    {ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED}
)
"""Statuses that, on a non-terminal observation, say the executor did not run the request."""


class RejectionCode(StrEnum):
    """Rejection code lifecycle contract."""

    STALE_VIEW = "stale-view"
    IDENTITY_CONFLICT = "identity-conflict"
    UNKNOWN_KIND = "unknown-kind"
    UNKNOWN_SCHEMA = "unknown-schema"
    UNDECLARED_OPERATION = "undeclared-operation"
    CAPABILITY = "capability"
    OWNERSHIP = "ownership"
    GENERATION = "generation"
    CLOSED_SCOPE = "closed-scope"
    BUDGET = "budget"
    DEPENDENCY = "dependency"
    EVIDENCE = "evidence"
    ALREADY_SETTLED = "already-settled"
    NOT_IMPLEMENTED_IN_KERNEL = "not-implemented-in-kernel"


class Area(StrEnum):
    """Area lifecycle contract."""

    SCHEDULING = "scheduling"
    ATTEMPTS = "attempts"
    SESSIONS = "sessions"
    EVALUATION = "evaluation"
    SETTLEMENT = "settlement"
    RECORDS = "records"


class KernelNotImplementedError(Exception):
    """Typed wave-1 rejection; callers may identify the exact owning lane."""

    code = RejectionCode.NOT_IMPLEMENTED_IN_KERNEL

    def __init__(self, area: Area, event_kind: str, subarea: str | None = None) -> None:
        """Name the owning area and its unimplemented event."""
        self.area = area
        self.event_kind = event_kind
        self.subarea = subarea
        owner = f"{area.value}/{subarea}" if subarea is not None else area.value
        super().__init__(f"{owner}: {event_kind} not implemented in kernel")


class SignalCycleError(Exception):
    """A reducer re-emitted a signal already consumed in this transition."""


class InterruptPolicy(StrEnum):
    """What core does with a step whose attempt was interrupted without a result."""

    RERUN = "rerun"
    """Run it again as the next attempt."""

    CANCEL = "cancel"
    """Settle it as ``Cancelled(INTERRUPTED)``: a person watched the attempt and decides."""


class ClosePolicy(StrEnum):
    """What a scope that is closing does with an open step of its own scope.

    Every kind of step declares one (``request_close_policy``), so the rule never names a kind.
    A step the close depends on is waited for; a step only the scope would read is withdrawn.
    """

    WAIT = "wait"
    """The close waits for the step to end by itself or at its limit: cleanup the close depends
    on, work whose result something still reads. The default."""

    WITHDRAW = "withdraw"
    """The close cancels the step if it runs and settles it if it does not: nothing outside the
    scope reads its result, so waiting for it only keeps the scope open."""


class StopPolicy(StrEnum):
    """What a stop does with an open step: a run ending by a cancel, and a Pause-now.

    Every kind of step declares one (``request_stop_policy``), so no rule names a kind.
    """

    WORK = "work"
    """Work nobody wants once the run stops: a stop cancels it if it runs and settles it if it
    has not started. The default."""

    KEEP = "keep"
    """Work a turn already did (a snapshot, a retain, evidence, a block): a Pause-now lets it
    finish, so the next epoch finds it done. A run ending by a cancel does not want it."""

    RELEASE = "release"
    """The step frees what the run holds (a session, a workspace, a job, a scope): every stop
    lets it run on to its result, within its own limit."""


class OperationSchemaRef(Value):
    """Operation schema ref lifecycle contract."""

    # Fields declared with this class hold exactly this class (a subclass instance is rebuilt as
    # one on validation), so one remembered JSON text per instance holds wherever it sits.
    model_config = ConfigDict(revalidate_instances="subclass-instances")

    kind: str = Field(min_length=1)
    request_schema: SchemaRef
    outcome_schema: SchemaRef
    lifecycle: LifecycleClass
    on_interrupt: InterruptPolicy = Field(
        default=InterruptPolicy.RERUN, exclude_if=lambda policy: policy is InterruptPolicy.RERUN
    )
    """The asker knows whether a person watches the turn; only a session turn may cancel."""

    @model_validator(mode="after")
    def only_turns_cancel(self) -> Self:
        """A step that nobody watches is always run again."""
        if (
            self.on_interrupt is InterruptPolicy.CANCEL
            and self.lifecycle != LifecycleClass.SESSION_TURN
        ):
            raise ContractValidationError("on_interrupt", "only a session turn may cancel")
        return self


class RevisionAuthority(StrEnum):
    """Explicit workspace authority, never inferred from operation kind names."""

    NONE = "none"
    SNAPSHOT = "snapshot"
    RETAIN = "retain"
    RESTORE = "restore"
    DISCARD = "discard"


class OperationNormalizationKind(StrEnum):
    """Closed normalization routes; pure normalizers live outside state."""

    NONE = "none"
    SCOPE_REOPEN = "scope_reopen"


class ScopeReopenNormalization(Value):
    """Canonical guarded reopen target bound to a registered payload.

    The operation is run-scoped. Its target must be the exact parked attempt,
    and resolved_cancelled_jobs must equal the continuation cancellation set.
    A newer park authority invalidates older reopen acknowledgements.
    """

    kind: Literal["scope_reopen"] = "scope_reopen"
    attempt: AttemptRef
    continuation_id: ContinuationId
    park_authority: RequestId
    resolved_cancelled_jobs: tuple[ResourceId, ...]

    @model_validator(mode="after")
    def unique_resolutions(self) -> ScopeReopenNormalization:
        """Reject duplicate cancellation-resolution claims at ingress."""
        if len(set(self.resolved_cancelled_jobs)) != len(self.resolved_cancelled_jobs):
            raise ContractValidationError("resolved_cancelled_jobs", "duplicate resource ID")
        return self


class OperationDescriptor(OperationSchemaRef):
    """Operation descriptor lifecycle contract."""

    normalization: OperationNormalizationKind = OperationNormalizationKind.NONE
    inspect: bool = False
    cancel: bool = False
    watch: bool = False
    resource_pool: PoolId | None = None
    revision_authority: RevisionAuthority = RevisionAuthority.NONE
    on_close: ClosePolicy = Field(
        default=ClosePolicy.WAIT, exclude_if=lambda policy: policy is ClosePolicy.WAIT
    )
    """Whether a scope that is closing withdraws this operation or waits for it; it waits by default."""

    @model_validator(mode="after")
    def lifecycle_contract(self) -> OperationDescriptor:
        """Require explicit resource and revision ownership declarations."""
        if self.lifecycle == LifecycleClass.OWNED_JOB and self.resource_pool is None:
            raise OperationDescriptorError("resource_pool", "owned jobs require a declared pool")
        if self.lifecycle != LifecycleClass.OWNED_JOB and self.resource_pool is not None:
            raise OperationDescriptorError("resource_pool", "only owned jobs declare pools")
        if (
            self.revision_authority != RevisionAuthority.NONE
            and self.lifecycle != LifecycleClass.IDEMPOTENT_WRITE
        ):
            raise OperationDescriptorError(
                "revision_authority", "revision mutations require idempotent writes"
            )
        if self.normalization == OperationNormalizationKind.SCOPE_REOPEN and (
            self.lifecycle != LifecycleClass.IDEMPOTENT_WRITE
            or not self.inspect
            or self.resource_pool is not None
            or self.revision_authority != RevisionAuthority.NONE
        ):
            raise OperationDescriptorError(
                "normalization", "scope reopen requires inspectable non-revision idempotent write"
            )
        return self


class OperationDescriptorError(ValueError):
    """Invalid operation ownership contract."""

    def __init__(self, path: str, detail: str) -> None:
        """Name the invalid declaration."""
        super().__init__(f"{path}: {detail}")


class Capabilities(Value):
    """Capabilities lifecycle contract."""

    lifecycle: frozenset[LifecycleCapability] = frozenset()
    operations: tuple[OperationDescriptor, ...] = ()

    @field_serializer("lifecycle", when_used="json")
    def _sorted_lifecycle(self, value: frozenset[LifecycleCapability]) -> list[str]:
        # A frozenset iterates in hash order, which varies with insertion history and the
        # string hash seed, so two equal sets could otherwise be written differently.
        """Write the set sorted, so equal sets give equal JSON."""
        return sorted(value)


MAX_SUBMISSIONS_PER_MEASUREMENT = 5
MAX_CORRECTIONS = 5
"""The most corrections of one reply any role may ask for."""
MAX_LOST_TURNS = 5
"""The most times any role may have a lost turn asked again."""
MAX_REASK_CHAIN = MAX_CORRECTIONS + MAX_LOST_TURNS
"""The longest chain of corrections and re-asks any role's bounds allow; core refuses a longer one.

A re-ask after a turn lost to its provider (``PROVIDER_UNAVAILABLE``) is no link of the chain:
the run's capacity gate bounds an outage, by its budget and the pause that follows it."""


class TaskTerms(Value):
    """What the task fixes about measuring and comparing candidates.

    Core carries these terms and holds none of them as a default: the host resolves them from
    the task's input bundle and domain, and a search reads them through ``ctx.task``. The
    evaluator, workload and environment identities that go with them are the run facts'.
    """

    recipe: ArtifactRef
    """The stored evaluation plan every measurement plan cites."""
    stages: tuple[TaskStage, ...]
    """Every stage the task can measure, in dependency order, with its time limit."""
    measured: tuple[str, ...]
    """The stages a candidate is measured on, in order: the task's gates."""
    queue_allowance: float = Field(ge=0, allow_inf_nan=False)
    """Seconds a measurement may wait in the evaluation queue."""
    accuracy_stage: str | None = Field(default=None, min_length=1)
    """The stage whose pass is the correctness gate."""
    benchmark_stage: str | None = Field(default=None, min_length=1)
    """The stage whose benchmark reading a candidate is compared with its base on, if any.

    A task without one has no number to compare, so core measures no base for it
    (`vs_core._comparison`)."""
    accuracy_gated: tuple[EvidenceKind, ...] = ()
    """Evidence kinds that count only if the accuracy stage passed."""
    operation_seconds: float = Field(gt=0, allow_inf_nan=False)
    """Seconds one read of the evidence (decoding it, verifying a parent) may take."""
    objectives: tuple[TaskObjective, ...] = ()
    """The directed axes candidates are ranked on, headline first."""
    relative_noise: float = Field(default=0.0, ge=0.0, lt=1.0)
    """The benchmark's declared variation: a reading must beat the input by more than this."""
    base_staleness: float = Field(default=21_600.0, gt=0, allow_inf_nan=False)
    """Seconds after which a measured base no longer stands for the host it was timed on.

    A candidate's comparison reads only base samples this young, on the run clock, and a base
    with none fresh is measured again (`vs_core._comparison`)."""
    runaway_evaluations: int = Field(default=40, ge=1)
    """How many evaluations one attempt's agent may ask for before the run warns of it.

    Evaluations have no allowance, so this refuses nothing: it marks only an egregious count
    (`vs_core._runaway`)."""
    profile_fields: tuple[str, ...] = ()
    """The measurement fields the task's profile capture supplies."""

    @property
    def requires_accuracy_proof(self) -> bool:
        """Whether a candidate must carry an accepted accuracy proof to be adopted.

        Only a task that measures its accuracy stage can produce one; a task without that gate
        adopts what its other gates admit.
        """
        return self.accuracy_stage is not None and self.accuracy_stage in self.measured

    @model_validator(mode="after")
    def _stages_exist(self) -> TaskTerms:
        known = {item.stage_id for item in self.stages}
        named = {
            *self.measured,
            *([self.accuracy_stage] if self.accuracy_stage else []),
            *([self.benchmark_stage] if self.benchmark_stage else []),
        }
        if unknown := sorted(named - known):
            raise ContractValidationError("measured", f"names stages the task lacks: {unknown}")
        return self


class RunFacts(Value):
    """Run facts lifecycle contract."""

    objective: str
    options: tuple[PureOption, ...] = ()
    constraints: tuple[PureOption, ...] = ()
    baseline: RevisionRef
    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    dedicated_measurement_resources: bool = Field(default=False, exclude_if=unset)
    """Each measurement job gets its GPUs and CPU cores to itself, as the run's executor declares.

    The executor's capability, derived from how it submits jobs (a Slurm job with a bare
    ``--exclusive`` or an operator declaration), never from a backend's name. A run timed so is timed as an official
    measurement is, so only such a run may answer one (``vs_core._measurement_reuse``).
    """
    artifacts: tuple[ArtifactRef, ...] = ()
    task: TaskTerms | None = None
    """What the task fixes; a search that measures candidates reads it with ``ctx.task``."""


def escalate_stop(held: StopMode | None, asked: StopMode) -> StopMode:
    """The stop in force once ``asked`` arrives while ``held`` is: a stop only escalates.

    The one order of stop modes. A cancel replaces a drain and nothing replaces a cancel,
    so the requests' order never weakens a stop: every holder of a run's stop (the run's
    control channel, its bridge to core, core itself) folds requests through this.
    """
    return StopMode.CANCEL if StopMode.CANCEL in (held, asked) else StopMode.DRAIN


OPERATOR_ACTIONS: frozenset[ControlAction] = frozenset(
    {"cancel_attempt", "cancel_item", "set_max_parallel"}
)
"""The typed operator actions on core entities, which core applies alike in every search."""


class CompletionStatus(StrEnum):
    """Terminal semantic decision outcome, independent of dispatch acceptance."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DependencyStatus(StrEnum):
    """Readiness of durable completion and request dependencies."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RequestBase(Value):
    """Canonical I/O record correlation, including the current occupancy episode.

    Attempt mutations carry admission_id. Canonical registration derives a
    missing attempt episode from the exact owner and generation. Explicit older
    episodes authorize only recorded cleanup, never mutation of a newer episode.
    Delayed observations may complete that cleanup but cannot mutate later
    ownership. Referenced IDs are allocated before state or signals refer to them.
    """

    request_id: RequestId | None = None
    scope: Scope
    depends_on: tuple[RequestId, ...] = ()
    decision_id: DecisionId | None = None
    admission_id: DecisionId | None = None


class ChildManifest(Value):
    """Authoritative set of every transitive descendant at one observation.

    members lists all descendants, not only direct children. basis says how the
    executor knows it: "enumerated" from the live process or resource tree, or
    "lifecycle-closed" because the closed handle can admit no descendant (members
    may then be empty). Core requires every member to be owned and released
    before it accepts the release as complete.
    """

    members: tuple[ResourceId, ...] = ()
    basis: Literal["enumerated", "lifecycle-closed"]

    @model_validator(mode="after")
    def distinct_members(self) -> ChildManifest:
        """A resource appears at most once in the manifest."""
        if len(set(self.members)) != len(self.members):
            raise ContractValidationError("members", "duplicate resource ID")
        return self


class Observation(Value):
    """External facts correlated by request, scope, generation and episode.

    children_complete explicitly claims an authoritative manifest; an empty
    children tuple alone does not prove it. child_manifest, when present, is
    that manifest: it must contain every child, only an observation that claims
    children_complete may carry it, and consumers read descendants, never
    children alone, when proving release. A children_complete claim without a
    manifest stays accepted for producers that predate it. Install discovered child ownership
    before removing provisional request ownership. Unknown acceptance or missing
    identity never proves release. revision is the revision a snapshot or retain
    request produced or retained, as the executor saw it; a caller-supplied
    revision on a derived event is accepted only when it equals this one.
    """

    event_id: EventId
    request_id: RequestId
    scope: Scope
    sequence: Count
    observed_at: Seconds
    status: ObservationStatus
    resource_id: ResourceId | None = None
    revision: RevisionRef | None = None
    measured_input: str | None = Field(default=None, min_length=1, exclude_if=unset)
    """The digest of what an evaluation reads at ``revision``, when the runtime computed it."""
    accepted: bool = False
    terminal: bool = False
    released: bool = False
    children: tuple[ResourceId, ...] = ()
    children_complete: bool = False
    child_manifest: ChildManifest | None = None
    admission_id: DecisionId | None = None
    diagnostic: str = ""

    @property
    def retryable(self) -> bool:
        """A failure the executor reports as not run and not final: send the request again."""
        return not self.terminal and self.status in RETRYABLE_FAILURES

    @property
    def descendants(self) -> tuple[ResourceId, ...]:
        """Discovered children plus every manifest member, without duplicates."""
        manifest = () if self.child_manifest is None else self.child_manifest.members
        return tuple(dict.fromkeys((*self.children, *manifest)))

    @model_validator(mode="after")
    def manifest_covers_children(self) -> Observation:
        """A manifest is a complete claim and cannot omit a reported child."""
        if self.child_manifest is None:
            return self
        if not self.children_complete:
            raise ContractValidationError("child_manifest", "requires children_complete")
        if not set(self.children) <= set(self.child_manifest.members):
            raise ContractValidationError("child_manifest", "omits a reported child")
        return self


class ChargeReceipt(Value):
    """Authoritative charge and bounded, deduplicated refund proof.

    Historical aggregate proof preserves consumed migration budget only. It
    grants no dispatch authority and cannot receive a new refund without exact
    live charge correspondence. Unsupported ADMISSION refunds require positive
    nonacceptance and drained ownership; ATTEMPT refunds need bounded authority.
    """

    charge_id: ChargeId
    kind: ChargeKind
    invocation_id: InvocationId | None = None
    source_request: RequestId | None = None
    charged: Count
    refunded: Count = 0
    refund_sources: tuple[RequestId, ...] = ()
    historical_proof: ArtifactRef | None = None

    @model_validator(mode="after")
    def bounded_refund(self) -> ChargeReceipt:
        """Reject over-refunds, repeated authority and any TURN refund."""
        if self.refunded > self.charged:
            raise ContractValidationError("refunded", "exceeds charged amount")
        if self.kind == ChargeKind.TURN and self.refunded:
            raise ContractValidationError("refunded", "TURN charges never refund")
        if len(set(self.refund_sources)) != len(self.refund_sources):
            raise ContractValidationError("refund_sources", "duplicate refund authority")
        return self


class SetupFailureKind(StrEnum):
    """Setup classification; unknown acceptance requires inspection."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


def validate_setup_failure(observation: Observation, failure: SetupFailureKind) -> None:
    """Classification requires terminal failure and grants no acceptance proof.

    An accepted-but-failed setup retains its lease cleanup obligations. Refunds
    require separate positive nonacceptance and recorded charge authority.
    """
    if failure != SetupFailureKind.UNKNOWN and not (
        observation.terminal
        and observation.status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        raise ContractValidationError("setup_failure", "requires terminal setup failure")


class HostFence(Value):
    """Host fence lifecycle contract."""

    host_id: HostId
    epoch: Count


class EventCursor(Value):
    """Event cursor lifecycle contract."""

    sequence: Count


class IdentityTypeError(ValueError):
    """Different identity domains cannot be substituted."""

    def __init__(self) -> None:
        """Report a domain mismatch at validation ingress."""
        super().__init__("identity type mismatch")


class WorkspaceMode(StrEnum):
    """Workspace mode lifecycle contract."""

    EXCLUSIVE_ROOT = "exclusive-root"
    ISOLATED_CHILD = "isolated-child"
    READ_ONLY_REVISION = "read-only-revision"


class WorkspaceRef(Value):
    """Workspace ref lifecycle contract."""

    scope: Scope
    revision: RevisionRef
    mode: WorkspaceMode


class ReleaseDependency(Value):
    """An owned resource or disposition acknowledgement required for release."""

    kind: Literal["session", "job", "workspace", "operation", "request"]
    identity: SessionId | ResourceId | RequestId | OperationId

    @model_validator(mode="after")
    def domain_matches_kind(self) -> ReleaseDependency:
        """Reject accidental cross-domain release graph edges."""
        expected = {
            "session": SessionId,
            "job": ResourceId,
            "workspace": RequestId,
            "operation": OperationId,
            "request": RequestId,
        }
        if type(self.identity) is not expected[self.kind]:
            raise IdentityTypeError
        return self


class OperationWire(Value):
    """Operation wire lifecycle contract."""

    schema_ref: OperationSchemaRef
    payload_json: str


class ReservedInputOccurrence(Value):
    """Immutable execution payload for an input occurrence reserved by Sessions B.

    Sessions A validates the exact ID/artifact manifest against InputRecord before
    dispatch. The operation wire remains canonical and is never rewritten to
    transport inputs. Equal artifacts with distinct occurrence IDs stay distinct.
    """

    input_id: InputId
    artifact: ArtifactRef


class ExecuteRegisteredOperation(RequestBase):
    """Execute a canonical registered wire with separately reserved input payload.

    Sessions A resolves the accepted Operation origin and reserves its inputs
    before Records A registers this final executable payload. Dispatch uses that
    sole canonical manifest; changing it under the same request ID is a conflict.
    """

    kind: Literal["execute_registered_operation"] = "execute_registered_operation"
    operation_id: OperationId
    operation: OperationWire
    retry_limit: Count
    inputs: tuple[ReservedInputOccurrence, ...] = ()

    @model_validator(mode="after")
    def distinct_reserved_occurrences(self) -> ExecuteRegisteredOperation:
        """An occurrence may appear once even when artifacts repeat."""
        if self.inputs and self.operation.schema_ref.lifecycle != LifecycleClass.SESSION_TURN:
            raise ContractValidationError(
                "inputs", "reserved inputs require SESSION_TURN ownership"
            )
        identities = tuple(item.input_id for item in self.inputs)
        if len(set(identities)) != len(identities):
            raise ContractValidationError("inputs", "duplicate input_id")
        return self


class ActionRefusal(StrEnum):
    """Why core refuses an operator action: it could change nothing, or exceed the launch."""

    NO_SUCH_SUBJECT = "no_such_subject"
    """The run holds no such attempt, or no attempt at such an item."""
    ENDED = "ended"
    """The attempt has ended."""
    PARKED = "parked"
    """The attempt is blocked, or parked with no reopening queued: nothing a cancel could stop."""
    ALREADY_CANCELLED = "already_cancelled"
    """The item is already cancelled."""
    ABOVE_LAUNCH_LIMIT = "above_launch_limit"
    """A parallel limit above the one the run was launched with."""


ACTION_REFUSAL_TEXT: dict[ActionRefusal, str] = {
    ActionRefusal.NO_SUCH_SUBJECT: "the run holds no such attempt or item",
    ActionRefusal.ENDED: "the attempt has ended",
    ActionRefusal.PARKED: "the attempt is parked or blocked, so there is nothing to stop",
    ActionRefusal.ALREADY_CANCELLED: "the item is already cancelled",
    ActionRefusal.ABOVE_LAUNCH_LIMIT: "the limit is above the one the run was launched with",
}
"""The one wording of each refusal, wherever an operator reads it."""
