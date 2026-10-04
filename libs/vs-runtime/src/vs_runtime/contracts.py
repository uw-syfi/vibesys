"""Policy-facing contracts for the reusable orchestration runtime."""

from __future__ import annotations

import hashlib
import inspect
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import (
    Path,
    PurePosixPath,
    PureWindowsPath,
)  # Pydantic resolves WorkspaceRef at runtime.
from typing import TYPE_CHECKING, Annotated, Protocol, TypeVar, overload

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from vs_evaluator_protocol.api import PartialMeasurement

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_agent.api import AgentSessionCheckpoint, AgentSessionKey, InvocationOutcome
    from vs_evaluation.api import EvaluationSettlements
    from vs_project.api import OrchestrationDescriptor, StateModels
    from vs_prompts.api import RenderedPrompt

ResponseT = TypeVar("ResponseT", bound=BaseModel)
_CONTROL_CHARACTER_LIMIT = 32


def _is_concrete_model_class(value: object) -> bool:
    """Return whether a plugin schema is an instantiable Pydantic model class."""
    return (
        isinstance(value, type)
        and value is not BaseModel
        and issubclass(value, BaseModel)
        and not inspect.isabstract(value)
    )


class RuntimeContractError(RuntimeError):
    """Base class for typed runtime operation failures."""


class SessionTransportUnavailableError(RuntimeContractError):
    """Durable session operations require an explicitly bound agent interface."""


class RunCleanupError(RuntimeContractError):
    """Run-owned cleanup is unresolved; resource release is not confirmed.

    ``failures`` retains every underlying outcome, including cancellation and
    unknown external identity, for diagnostics and recovery. Raising this
    error never marks a release intent completed or proves job termination.
    """

    def __init__(self, message: str, failures: tuple[BaseException, ...]) -> None:
        """Retain cleanup failures without exposing an untyped exception group."""
        self.failures = failures
        detail = "; ".join(f"{type(failure).__name__}: {failure}" for failure in failures)
        super().__init__(f"{message}: {detail}")


class AgentTurnTimeoutError(RuntimeContractError):
    """An agent session turn exceeded its configured wall-clock budget."""

    def __init__(self, timeout_seconds: float) -> None:
        """Record the budget so orchestration policy can choose its response."""
        self.timeout_seconds = timeout_seconds
        super().__init__(f"agent turn timed out after {timeout_seconds:g} seconds")


class WorkspaceRestoreError(RuntimeContractError):
    """A workspace could not materialize a requested retained revision."""

    def __init__(self, revision: str) -> None:
        """Name the revision that could not be restored."""
        super().__init__(f"could not restore workspace revision {revision!r}")


class SessionClosedError(RuntimeContractError):
    """A caller used an agent session after its lifetime ended."""

    def __init__(self) -> None:
        """Describe a turn attempted after session cleanup."""
        super().__init__("agent session is closed")


class StructuredResponseError(RuntimeContractError):
    """An agent turn did not produce a valid response of its requested type.

    Raised both when the reply cannot be parsed and when the provider gave up
    producing output that matches the response schema. Either way the session
    keeps its conversation, so a caller can send a correction as the next
    turn. ``detail`` holds the validation errors when the provider reported
    them, and the message includes them.
    """

    def __init__(self, role_id: str, response_type: type[BaseModel], detail: str = "") -> None:
        """Name the role and response contract whose validation failed, and why if known."""
        self.detail = detail
        reason = f": {detail}" if detail else ""
        super().__init__(
            f"agent role {role_id!r} did not return a valid {response_type.__name__} "
            f"response{reason}"
        )


class UnknownAgentRoleError(RuntimeContractError):
    """A session was requested for a role outside the plugin's agent tuple."""

    def __init__(self, role_id: str) -> None:
        """Name the role not owned by the active plugin."""
        super().__init__(f"agent role {role_id!r} is not declared by this orchestration")


class StateModelError(RuntimeContractError):
    """A state operation did not use the plugin's exact declared model."""

    def __init__(self, expected: type[BaseModel] | None, actual: type[BaseModel]) -> None:
        """Name the declared and requested state models."""
        if expected is None:
            message = "this orchestration does not declare durable state"
        else:
            message = f"orchestration state requires {expected.__name__}, got {actual.__name__}"
        super().__init__(message)


class WorkspaceAccess(StrEnum):
    """Workspace mutation authority enforced for one role.

    ``LIMITED`` access requires session-specific writable paths. Paths that are
    directories when the session is created grant their descendants; file and
    nonexistent paths grant only the exact path.
    """

    READ_ONLY = "read_only"
    LIMITED = "limited"
    READ_WRITE = "read_write"


class AgentCapability(StrEnum):
    """Closed driver capabilities a role may require."""

    MCP_SERVERS = "mcp_servers"
    NESTED_READ_ONLY_PATHS = "nested_read_only_paths"
    HIDDEN_PATHS = "hidden_paths"
    HOST_PATH_GRANTS = "host_path_grants"
    CONTAINER_EXECUTION = "container_execution"
    TIMEOUTS = "timeouts"
    SESSION_REUSE = "session_reuse"
    PROVIDER_SESSION_RESUME = "provider_session_resume"
    DURABLE_TURN_CONTINUATION = "durable_turn_continuation"


class AgentTool(BaseModel):
    """Minimal immutable reference to one registered extra agent tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")


class Workspace(Protocol):
    """One live run workspace with semantic revision operations."""

    @property
    def id(self) -> str | None:
        """Return the isolated workspace ID, or ``None`` for the run root."""
        ...

    @property
    def path(self) -> Path:
        """Return the workspace's host path."""
        ...

    @property
    def revision(self) -> str | None:
        """Return the workspace's latest recorded revision, if one exists."""
        ...

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the immutable trusted-input baseline, if configured."""
        ...

    async def snapshot(self, label: str) -> str:
        """Record the current workspace tree and return its revision."""
        ...

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Materialize a retained revision or raise :class:`WorkspaceRestoreError`."""
        ...

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Try to materialize a revision, returning ``False`` on restore failure."""
        ...

    async def retain(self, revision: str, *, label: str) -> None:
        """Keep a revision reachable under a policy-owned semantic label."""
        ...

    async def pending_changes(self) -> list[str]:
        """List uncommitted workspace-relative paths."""
        ...


class CandidateWorkspace(Workspace, Protocol):
    """One isolated candidate workspace owned by its creating run."""

    async def discard(self) -> None:
        """Release the isolated workspace; safe to call more than once."""
        ...


class WorkspaceRef(BaseModel):
    """Immutable workspace value suitable for adapters and tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str | None = Field(default=None, min_length=1)
    path: Path


AgentRoleId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]


class AgentRole(BaseModel):
    """Complete immutable declaration of one policy-owned agent role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: AgentRoleId
    system_prompt: str
    extra_tools: tuple[AgentTool, ...] = ()
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_WRITE
    required_capabilities: frozenset[AgentCapability] = frozenset()


@dataclass(frozen=True, slots=True)
class AgentToolBindingContext:
    """Session identity available while resolving one declared agent tool.

    The runtime supplies the exact declared role, selected workspace, and
    optional durable member identity. Product composition can therefore issue
    a least-authority capability without exposing provider or backend details
    to orchestration policy.
    """

    role: AgentRole
    workspace: Workspace
    member_id: str | None
    agent_path: Callable[[Path], str]


class AgentBinding(BaseModel):
    """Immutable runtime choices resolved for one role session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str = Field(min_length=1)
    driver: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class AgentConversationOpenError(RuntimeContractError):
    """Conversation setup failed before any provider turn was dispatched."""


@dataclass(frozen=True)
class AgentConversationRequest:
    """In-process binding inputs, not a competing kernel SessionSpec or TurnSpec.

    Actual role and workspace handles bind immutable policy authority without
    allocating provider resources; kernel lifecycle intent remains authoritative.
    """

    role: AgentRole
    workspace: Workspace
    member_id: str
    generation: int | None = None
    invocation_id: str | None = None
    writable_paths: tuple[str, ...] = ()


class InvocationRelease(BaseModel):
    """Durable policy authorizes releasing this invocation after cancellation drains."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    invocation_id: str = Field(min_length=1)


class AgentConversation(Protocol):
    """Bound conversation without initialized harness or checkpoint guarantees.

    Inspection works before opening. Turns own setup and cancellation drain;
    close is idempotent even before opening. Generic cancellation preserves Unknown.
    """

    @property
    def role(self) -> AgentRole:
        """Return the immutable role bound before opening."""
        ...

    @property
    def workspace(self) -> Workspace:
        """Return the fixed workspace handle."""
        ...

    @property
    def member_id(self) -> str | None:
        """Return the durable policy identity, if one was bound."""
        ...

    @property
    def closed(self) -> bool:
        """Return whether further turns are rejected."""
        ...

    @property
    def session_key(self) -> AgentSessionKey:
        """Return the stable conversation identity before or after opening."""
        ...

    @property
    def invocation_id(self) -> str | None:
        """Return the immutable bound invocation, or None for an unbound conversation."""
        ...

    def inspect(self, invocation_id: str) -> InvocationOutcome:
        """Read evidence without treating unknown acceptance as replay authority."""
        ...

    async def resume(
        self,
        message: RenderedPrompt,
        invocation_id: str,
        *,
        response: type[BaseModel] | None = None,
    ) -> InvocationOutcome:
        """Continue the bound conversation and retain its durable acceptance fence."""
        ...

    @overload
    async def turn(
        self, message: str, *, response: None = None, invocation_id: str | None = None
    ) -> str: ...

    @overload
    async def turn(
        self, message: str, *, response: type[ResponseT], invocation_id: str | None = None
    ) -> ResponseT: ...

    async def close(self) -> None:
        """Drain owned operations and close resources once, including before opening."""
        ...


class PreparedConversation(AgentConversation, Protocol):
    """Deferred conversation that accepts durable release authority after drain."""

    def authorize_release(self, authority: InvocationRelease) -> None:
        """Bind live policy authority; apply it only after runtime cancellation drains."""
        ...


class AgentSession(AgentConversation, Protocol):
    """Initialized conversation with fixed grants and resolved harness attribution."""

    @property
    def writable_paths(self) -> tuple[str, ...]:
        """Return the fixed workspace-relative write grants for this session."""
        ...

    @property
    def binding(self) -> AgentBinding:
        """Return immutable harness and model attribution resolved by the runtime."""
        ...

    def checkpoint(self) -> AgentSessionCheckpoint:
        """Return provider checkpoint identity or a typed session error."""
        ...

    def release_interrupted(self, invocation_id: str) -> None:
        """Permit a new turn after an explicitly interrupted turn has drained."""
        ...


class WorkspaceAgentSessions(Protocol):
    """Run-owned factory and lifetime owner for agent conversations."""

    def prepare_conversation(self, request: AgentConversationRequest) -> PreparedConversation:
        """Bind fixed policy inputs without opening a provider conversation."""
        ...

    def inspect_invocation(self, key: AgentSessionKey, invocation_id: str) -> InvocationOutcome:
        """Inspect the authoritative journal before opening provider resources."""
        ...

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        generation: int | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        """Create a conversation with fixed write grants.

        ``member_id`` enables durable provider-session resume. A positive
        ``generation`` gives that member a separate durable conversation.
        Omitting it preserves its existing stable conversation. ``writable_paths``
        is required only for ``LIMITED`` roles and is forbidden for the other
        access modes.
        """
        ...

    async def close(self) -> None:
        """Close every owned session in reverse creation order."""
        ...


class Workspaces(Protocol):
    """Run-owned access to the root and isolated candidate workspaces."""

    @property
    def root(self) -> Workspace:
        """Return the live root workspace for this run."""
        ...

    @property
    def supports_parallel_candidates(self) -> bool:
        """Return whether independent candidate workspaces can run concurrently."""
        ...

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
    ) -> CandidateWorkspace:
        """Create an isolated candidate from a retained revision or the root head.

        Without ``member_id`` every candidate gets a fresh path. With it, the
        path is a fixed function of the member ID, so a member that works in a
        sequence of candidates (one at a time, each from a new revision) keeps
        one path, and an agent session created with the same ``member_id``
        continues its provider conversation, whose provider keys history by
        working directory. At most one live candidate exists per member ID;
        creating a second while the first is live raises
        :class:`RuntimeContractError`.
        """
        ...

    async def adopt(self, revision: str) -> None:
        """Materialize a retained candidate revision in the root workspace."""
        ...

    async def export_patch(self, revision: str) -> str:
        """Export a retained revision against the trusted-input baseline."""
        ...


class CommandResult(BaseModel):
    """Bounded output from one sandboxed argv invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: str
    exit_code: int | None = None
    truncated: bool = False


class Commands(Protocol):
    """Sandboxed process execution scoped to a live run workspace."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Run one argv without policy-authored shell composition."""
        ...

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Run argv with a runtime-owned output file and return its bounded contents.

        The runtime appends ``output_argument`` and the temporary path, reads
        the file after a successful command, and attempts to remove it on every
        path. A failed command returns its ordinary diagnostic output and exit
        status. Failure to remove the artifact raises ``RuntimeContractError``
        unless another exception is already propagating, in which case the
        cleanup failure is attached as an exception note.
        """
        ...

    async def run_trusted_shell(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Run one audited shell recipe exactly as declared.

        This surface is for trusted evaluator inputs, not agent-authored policy
        commands that have not passed an independent approval gate.
        """
        ...


class SkillResourceRequest(BaseModel):
    """Policy-neutral request to resolve resources from one installed skill."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1)
    resource_paths: tuple[str, ...] = ()
    purpose: str = Field(min_length=1)


class ResolvedSkillResources(BaseModel):
    """Agent-visible paths resolved for one installed skill request."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1)
    router_path: str = Field(min_length=1)
    resource_paths: tuple[str, ...] = ()
    purpose: str = Field(min_length=1)


class SkillResolution(BaseModel):
    """Resolved skill resources and non-fatal selection diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolved: tuple[ResolvedSkillResources, ...] = ()
    diagnostics: tuple[str, ...] = ()


class SkillCatalogError(RuntimeContractError):
    """The installed skill catalog could not be read or validated."""


class Skills(Protocol):
    """Run-owned resolution of policy-selected installed skill resources."""

    async def resolve(self, requests: tuple[SkillResourceRequest, ...]) -> SkillResolution:
        """Resolve valid requests, preserving partial success and diagnostics.

        Raises :class:`SkillCatalogError` when the installed catalog cannot be
        built. Invalid individual skill names or resources are instead omitted
        from ``resolved`` and described in ``diagnostics``.
        """
        ...


def validate_workspace_writable_paths(
    access: WorkspaceAccess,
    writable_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate and freeze the session's bounded workspace write grants.

    Read-only and read-write roles use their complete access mode and therefore
    cannot also declare grants. Limited roles must name at least one canonical,
    workspace-relative POSIX path. Grants are fixed for the session lifetime.
    """
    if not isinstance(writable_paths, tuple):
        message = "writable_paths must be a tuple of workspace-relative paths"
        raise TypeError(message)
    if access is WorkspaceAccess.LIMITED:
        if not writable_paths:
            message = "limited workspace access requires writable_paths"
            raise ValueError(message)
    elif writable_paths:
        message = f"{access.value} workspace access cannot declare writable_paths"
        raise ValueError(message)

    seen: set[str] = set()
    for value in writable_paths:
        if not isinstance(value, str) or not value:
            message = "writable path entries must be nonempty workspace-relative paths"
            raise ValueError(message)
        posix_path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or value in {".", ".."}
            or "\\" in value
            or posix_path.as_posix() != value
            or any(part in {".", ".."} for part in posix_path.parts)
            or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value)
        ):
            message = f"writable path must be a canonical workspace-relative path: {value!r}"
            raise ValueError(message)
        if value in seen:
            message = f"duplicate writable path: {value!r}"
            raise ValueError(message)
        seen.add(value)
    return writable_paths


class State(Protocol):
    """Typed opaque policy-state durability bound to one plugin declaration."""

    def namespace(self, name: str) -> StateModels:
        """Open a strict machine-local host subsystem namespace for this run.

        These subsystem records do not enlarge the plugin snapshot contract.
        Invalid names raise ProjectStateError; stored models validate strictly on reads. The run host fence owns mutations.
        """
        ...

    async def load(self, model: type[ResponseT]) -> ResponseT | None:
        """Load state only when ``model`` is the plugin's exact declared type."""
        ...

    async def commit(
        self,
        value: BaseModel,
        *,
        workspace: Workspace | None = None,
        label: str | None = None,
    ) -> None:
        """Durably replace state, optionally atomically including the root workspace."""
        ...


class Control(Protocol):
    """Cooperative operator-control boundary for plugin control flow."""

    async def checkpoint(self) -> None:
        """Land a pending stop or wait for a pending pause to resume."""
        ...


class ProfileExecution(StrEnum):
    """Where profiling must execute to observe the production path."""

    LOCAL = "local"
    REMOTE = "remote"


class WorkspaceSourceFact(BaseModel):
    """Minimal immutable display facts for one pinned workspace source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    dest: str = Field(min_length=1)


class SkillFact(BaseModel):
    """One installed skill the run offers its agents.

    ``name`` is the agent-visible skill name, so ``<name>/SKILL.md`` is the
    skill's router in every agent workspace. ``description`` is the skill's
    frontmatter description on one line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class RunFacts(BaseModel):
    """Immutable prompt-visible facts resolved before orchestration starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    environment_notes: str = ""
    profile_execution: ProfileExecution = ProfileExecution.LOCAL
    objective_location: str = Field(default="OBJECTIVE.md", min_length=1)
    reference_location: str = Field(default=".", min_length=1)
    accuracy_command: str | None = Field(default=None, min_length=1)
    benchmark_command: str | None = Field(default=None, min_length=1)
    accuracy_configured: bool = False
    benchmark_configured: bool = False
    profiler_id: str = Field(default="none", min_length=1)
    workspace_sources: tuple[WorkspaceSourceFact, ...] = ()
    # The same installed catalog that ``Run.skills`` resolves against.
    skills: tuple[SkillFact, ...] = ()


class MetricDirection(StrEnum):
    """Which direction improves one benchmark objective."""

    MAXIMIZE = "max"
    MINIMIZE = "min"


class BenchmarkObjective(BaseModel):
    """One policy-selected metric axis for benchmark interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    direction: MetricDirection


class AccuracyReceipt(BaseModel):
    """Opaque proof that accuracy passed for an exact candidate revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    workspace_id: str | None = Field(default=None, min_length=1)
    revision: str = Field(min_length=1)


class AccuracyEvaluation(BaseModel):
    """Semantic outcome of the trusted accuracy evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    feedback: str | None = None
    receipt: AccuracyReceipt | None = None

    @property
    def passed(self) -> bool:
        """Return whether policy may accept this accuracy outcome."""
        return self.feedback is None


class BenchmarkFailureKind(StrEnum):
    """Whether a benchmark failure describes its workload or execution infrastructure."""

    WORKLOAD = "workload"
    INFRASTRUCTURE = "infrastructure"


class BenchmarkEvaluation(BaseModel):
    """Semantic outcome and measurements from the trusted benchmark."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    feedback: str | None = None
    # Absent for older or reused evidence that did not retain failure provenance.
    failure_kind: BenchmarkFailureKind | None = None
    metric_name: str | None = None
    metric_value: FiniteFloat | None = None
    metric_direction: MetricDirection | None = None
    metric_unit: str | None = None
    row: Mapping[str, FiniteFloat] | None = None
    # What a failed benchmark measured before it stopped, as its evaluator
    # reported it; absent when the evaluator reported nothing.
    partial_measurement: PartialMeasurement | None = None

    @model_validator(mode="after")
    def _partial_only_when_failed(self) -> BenchmarkEvaluation:
        if self.partial_measurement is not None and self.feedback is None:
            message = "only a failed benchmark carries a partial measurement"
            raise ValueError(message)
        return self

    @property
    def passed(self) -> bool:
        """Return whether policy may accept this benchmark outcome."""
        return self.feedback is None


class LocalValidationEvaluation(BaseModel):
    """Semantic outcome of candidate-authored local validation recipes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    feedback: str | None = None
    report_location: str | None = Field(default=None, min_length=1)
    recipe_unusable: bool = Field(
        default=False,
        description=(
            "The agent-reported recipe artifact could not be read, so no recipe ran. This "
            "is an input error the agent can correct in its reply, not a candidate failure."
        ),
    )

    @model_validator(mode="after")
    def _consistent_outcome(self) -> LocalValidationEvaluation:
        """Keep pass/fail state and policy-facing feedback unambiguous."""
        if self.passed == (self.feedback is not None):
            message = "passing local validation cannot have feedback; failure requires feedback"
            raise ValueError(message)
        if self.recipe_unusable and self.passed:
            message = "a passing local validation cannot report an unusable recipe"
            raise ValueError(message)
        return self


class AgentEvaluationStatus(StrEnum):
    """Lifecycle of one evaluation an agent submitted through its evaluation tool."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    CANCELED = "canceled"


class AgentEvaluationStageOutcome(StrEnum):
    """Trusted conclusion of one finished stage of an agent-submitted evaluation."""

    PASSED = "passed"
    FAILED = "failed"
    # The stage recorded measurements without a pass or fail verdict.
    OBSERVED = "observed"


class AgentEvaluationMetric(BaseModel):
    """One measurement a finished stage recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    value: FiniteFloat
    unit: str | None = None
    direction: MetricDirection | None = None


class AgentEvaluationStage(BaseModel):
    """The trusted outcome of one finished stage (accuracy, benchmark, ...)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    outcome: AgentEvaluationStageOutcome
    metrics: tuple[AgentEvaluationMetric, ...] = ()
    # What a failed stage measured before it stopped, as its evaluator reported it.
    partial_measurement: PartialMeasurement | None = None


class AgentEvaluation(BaseModel):
    """One trusted evaluation an agent submitted from a workspace.

    The host runs it, so its outcome is trusted even though an agent chose
    when to submit. ``failure`` is the complete failure text, present exactly
    when the evaluation failed. ``stages`` holds the outcome of each stage that
    finished with trusted evidence, in stage order; a stage still running, or
    one that crashed without evidence, has no entry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str = Field(min_length=1, description="The workspace snapshot that was evaluated.")
    kinds: tuple[str, ...] = Field(min_length=1, description="Evaluated evidence kinds.")
    status: AgentEvaluationStatus
    stages: tuple[AgentEvaluationStage, ...] = ()
    content_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "SHA-256 of the evaluated revision's patch as Workspaces.export_patch returns it: "
            "two revisions with this digest hold the same candidate content."
        ),
    )
    failure: str | None = Field(default=None, min_length=1)
    signature: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Exception type and innermost source line of the failure, when it holds a "
            "traceback. Failures with equal signatures are the same defect."
        ),
    )

    @model_validator(mode="after")
    def _failure_iff_failed(self) -> AgentEvaluation:
        if (self.status is AgentEvaluationStatus.FAILED) != (self.failure is not None):
            message = "a failed agent evaluation requires its failure, and only it has one"
            raise ValueError(message)
        if self.signature is not None and self.failure is None:
            message = "only a failed agent evaluation has a failure signature"
            raise ValueError(message)
        return self


class CandidateProfileStatus(StrEnum):
    """How one policy-requested profile of a candidate revision ended."""

    # The profiler observed the candidate and reported a diagnosis.
    OBSERVED = "observed"
    # The profiler ran but reported that it cannot profile this candidate.
    UNSUPPORTED = "unsupported"
    # No report: the profiler turn failed, was canceled or interrupted, or no
    # profiler is provisioned for the run.
    FAILED = "failed"


class CandidateProfileComponent(BaseModel):
    """The share of observed cost the profiler attributed to one component."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    share: FiniteFloat = Field(ge=0, le=1)


class CandidateProfile(BaseModel):
    """The trusted outcome of one profile that policy requested for a revision.

    ``operation_id`` names the host-owned profiler operation, the same record
    agents read through their trusted operations, and is ``None`` only when no
    operation started. ``failure`` is the framework's account of a failure and
    is present exactly when the profile failed; ``diagnosis`` is the profiler's
    report and is absent then.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str = Field(min_length=1)
    status: CandidateProfileStatus
    operation_id: str | None = Field(default=None, min_length=1)
    diagnosis: str | None = None
    components: tuple[CandidateProfileComponent, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    failure: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _failure_iff_failed(self) -> CandidateProfile:
        failed = self.status is CandidateProfileStatus.FAILED
        if failed != (self.failure is not None):
            message = "a failed candidate profile requires its failure, and only it has one"
            raise ValueError(message)
        if failed and (self.diagnosis is not None or self.components or self.evidence_ids):
            message = "a failed candidate profile carries no report"
            raise ValueError(message)
        return self


class ReleasedJobs(BaseModel):
    """What one :meth:`Evaluation.release_jobs` call cancelled for a member.

    ``evaluations`` and ``profiler_operations`` name the queued and running
    evaluation handles and profiler operations whose cancellation this call
    requested. ``first_release`` is False when the member's jobs were already
    released; that call cancelled nothing and both tuples are empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    member_id: str
    evaluations: tuple[str, ...]
    profiler_operations: tuple[str, ...]
    first_release: bool


class Evaluation(Protocol):
    """Trusted candidate evaluation effects available to policy."""

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        """Evaluate correctness, optionally reusing an exact-candidate pass."""
        ...

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
    ) -> BenchmarkEvaluation:
        """Measure one live workspace against policy-selected objectives."""
        ...

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Run audited candidate-authored recipes without permitting workspace mutation."""
        ...

    async def agent_evaluations(self, workspace: Workspace) -> tuple[AgentEvaluation, ...]:
        """Return the evaluations agents submitted from ``workspace``, oldest first.

        A candidate workspace keeps its identity across the attempts of one
        member, so the history spans them. Empty when the run offers agents no
        evaluation tool.
        """
        ...

    def settlements(self) -> EvaluationSettlements:
        """Return owned host observations without invoking an agent.

        Runs without agent evaluation tools raise RuntimeContractError.
        Cancelling an observation preserves evaluation jobs and ownership.
        """
        ...

    def current_time(self) -> float:
        """Return UTC logical time used by durable evaluation deadlines."""
        ...

    async def wait_until(self, deadline_at_s: float) -> None:
        """Suspend the host until absolute time reaches a recorded deadline."""
        ...

    async def submitted_generation(self, handle_id: str) -> int:
        """Read immutable submission ownership; settlements validate current ownership."""
        ...

    async def submitted_deadline(self, handle_id: str) -> float:
        """Read the absolute epoch deadline captured by the submitted plan."""
        ...

    async def cancel_submitted(self, handle_id: str) -> None:
        """Request cancellation for an immutable submitted evaluation."""
        ...

    async def accepted_evidence_ids(self, handle_id: str) -> tuple[str, ...]:
        """Read only backend-accepted semantic evidence for this exact handle."""
        ...

    async def submitted_report(self, handle_id: str) -> str:
        """Read the canonical immutable record, including retired generations.

        The backend validates captured identity before serializing its record.
        This historical read grants no observation, dispatch or resume authority.
        """
        ...

    async def submitted_revision(self, handle_id: str) -> str:
        """Read the immutable submitted capture, separately from retained WIP.

        An absent or inconsistent capture raises a typed contract error.
        """
        ...

    async def can_profile(self) -> bool:
        """Return whether :meth:`profile` can produce trusted profile evidence in this run.

        True only when the run provisions a profiler agent and its evaluation
        executor produces profile evidence. Policy offers profiling only when
        this holds; otherwise every profile ends unsupported or failed.
        """
        ...

    async def profile(self, revision: str, request: str, *, member_id: str) -> CandidateProfile:
        """Profile ``revision`` through the run's profiler agent and wait for its outcome.

        The profile is a host-owned profiler operation recorded under
        ``member_id``, so it is listed with the run's trusted operations.
        Every way the profile can end, including a run without a provisioned
        profiler, is a typed outcome. It raises only when the run, not the
        profile, ends the operation: on cancellation, and with ``RunStopped``
        when a stop or the host closing interrupts it. Such a profile has no
        outcome and runs again on resume.
        """
        ...

    async def reopen_jobs(self, member_id: str) -> None:
        """Reconcile a completed release and open a fresh generation for resumed work."""
        ...

    async def jobs_released(self, member_id: str) -> bool:
        """Project whether the member's durable scope refuses ordinary admission.

        Closing and completed releases both fence new work. Recovery can
        reconcile cleanup before opening a fresh scope generation.
        """
        ...

    async def release_jobs(self, member_id: str) -> ReleasedJobs:
        """Cancel ``member_id``'s cluster jobs and refuse its new ones.

        After it returns, the member's queued and running cluster jobs are
        cancelled: the evaluations its agents submitted from its workspace
        scope, its agents' profiler operations, and the profiles
        :meth:`profile` runs for it. New evaluation submissions and profiler
        dispatches from the member's scope, and new :meth:`profile` calls for
        it, are refused with a typed reply or outcome. Release is cleanup, so
        it works after a stop. Idempotent: a retry reconciles unfinished cleanup;
        ``first_release`` reports whether this call created the release intent.
        """
        ...


class RunStatus(StrEnum):
    """Terminal status returned by orchestration policy."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Observations(Protocol):
    """Publish plugin-authored live observations outside durable policy state."""

    def note(self, message: str) -> None:
        """Publish one informational note for the run operator."""
        ...

    def warning(self, message: str) -> None:
        """Publish one non-fatal policy warning for the run operator."""
        ...


@dataclass(frozen=True, slots=True)
class Run:
    """One runtime-created capability value passed to orchestration policy."""

    run_id: str
    facts: RunFacts
    agents: WorkspaceAgentSessions
    workspaces: Workspaces
    evaluation: Evaluation
    state: State
    control: Control
    commands: Commands
    skills: Skills
    observations: Observations


@dataclass(frozen=True, slots=True)
class OrchestrationResumeDecision:
    """A plugin-approved descriptor update and its workspace precondition."""

    descriptor: OrchestrationDescriptor | None
    requires_clean_workspace: bool = False


@dataclass(frozen=True, slots=True)
class OrchestrationPlugin:
    """One validated orchestration and every policy value it owns.

    ``agents`` is the sole role catalog. Runtime implementations may construct
    a private lookup from it, but no second public registry can disagree.
    """

    id: str
    agents: tuple[AgentRole, ...]
    options: type[BaseModel]
    orchestrate: Callable[[Run, BaseModel], Awaitable[RunStatus]]
    config_version: int = 1
    state: type[BaseModel] | None = None
    memory_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject duplicate role IDs before any run resources open."""
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", self.id) is None:
            message = f"invalid orchestration plugin ID {self.id!r}"
            raise ValueError(message)
        if self.config_version < 1:
            message = "orchestration plugin config version must be positive"
            raise ValueError(message)
        if not _is_concrete_model_class(self.options):
            message = "orchestration plugin options must be a concrete BaseModel subclass"
            raise ValueError(message)
        if self.state is not None and not _is_concrete_model_class(self.state):
            message = "orchestration plugin state must be a concrete BaseModel subclass"
            raise ValueError(message)
        seen: set[str] = set()
        duplicates: set[str] = set()
        for role in self.agents:
            if role.id in seen:
                duplicates.add(role.id)
            seen.add(role.id)
        if duplicates:
            message = f"duplicate agent role IDs: {', '.join(sorted(duplicates))}"
            raise ValueError(message)


_AGENT_ID_MAX_LENGTH = 128


def _agent_id_violation(value: str) -> str | None:
    """Return why ``value`` is not a canonical agent identifier, or ``None`` if it is.

    An agent identifier is a name an agent chose that the framework later uses
    as a key: a member ID, a workspace and Git ref name, a state namespace, a
    lookup ID. It must have exactly one spelling, so two strings that look the
    same or that a consumer would normalize to one value are never two
    identifiers. Every character is printable (no control, format, surrogate,
    private-use, or unassigned characters, and no whitespace except the ASCII
    space), the text is in Unicode NFC form, and it neither starts nor ends
    with a space. The check rejects instead of normalizing: normalizing would
    merge distinct identifiers and leave the agent unaware of the name it
    must use later.
    """
    if not value:
        return "it is empty"
    if not value.isprintable():
        hidden = next(character for character in value if not character.isprintable())
        return (
            f"it contains the non-printable character U+{ord(hidden):04X}; use only "
            "printable characters, with the ASCII space as the only whitespace"
        )
    if value != value.strip():
        return f"it has leading or trailing whitespace; use {value.strip()!r}"
    if not unicodedata.is_normalized("NFC", value):
        return f"it is not in Unicode NFC form; use {unicodedata.normalize('NFC', value)!r}"
    return None


def _checked_agent_id(value: str) -> str:
    if (violation := _agent_id_violation(value)) is not None:
        message = f"{value!r} is not a valid identifier: {violation}"
        raise ValueError(message)
    return value


# An agent-supplied identifier, parsed where agent output enters the system:
# 1 to 128 printable characters in NFC form without leading or trailing
# whitespace (rules and rationale in ``_agent_id_violation``). Validation errors
# name the value and the fix, so a correction turn can act on them.
AgentId = Annotated[
    str,
    Field(min_length=1, max_length=_AGENT_ID_MAX_LENGTH),
    AfterValidator(_checked_agent_id),
]


def validate_member_id(member_id: str | None) -> None:
    """Require a canonical agent identifier (see ``AgentId``) or ``None``.

    The length bound of ``AgentId`` is a schema rule for agent output and is
    not applied here: a longer member ID still maps to a valid workspace ID.
    """
    if member_id is None:
        return
    violation = (
        "it is not a string" if not isinstance(member_id, str) else _agent_id_violation(member_id)
    )
    if violation is not None:
        message = f"invalid agent member ID {member_id!r}: {violation}"
        raise ValueError(message)


def member_workspace_id(member_id: str) -> str:
    """Return the stable candidate workspace ID for one logical member.

    The ID is valid everywhere it is used: a path component, a project state
    namespace, and a Git ref component (``refs/vibesys/<run>/candidates/<id>``).
    It is a readable, lowercased prefix of the member ID plus a digest of the
    original ID, so members differing only in case or punctuation never share a
    path. The prefix uses only lowercase letters, digits, single dots,
    underscores, and hyphens: runs of dots become a hyphen because Git forbids
    ``..``. The ID starts with ``m-`` and ends with a hex digest, so it never
    starts with a dot or ends with ``.`` or ``.lock``. IDs that were already
    lowercase without dot runs keep their previous form.
    """
    validate_member_id(member_id)
    readable = re.sub(r"[^a-z0-9._-]+", "-", member_id.lower())
    readable = re.sub(r"\.{2,}", "-", readable).strip("-.")[:48]
    digest = hashlib.sha256(member_id.encode()).hexdigest()[:12]
    return f"m-{readable}-{digest}" if readable else f"m-{digest}"


def validate_command(argv: tuple[str, ...], timeout_seconds: int | None) -> None:
    """Reject malformed argv and timeout values before opening execution effects."""
    if not isinstance(argv, tuple) or not argv or not argv[0]:
        message = "command argv must be a nonempty tuple with a nonempty executable"
        raise ValueError(message)
    if any(not isinstance(argument, str) or "\0" in argument for argument in argv):
        message = "command argv must contain only NUL-free strings"
        raise ValueError(message)
    if timeout_seconds is not None and timeout_seconds <= 0:
        message = "command timeout must be positive"
        raise ValueError(message)


def validate_trusted_shell_command(command: str, timeout_seconds: int | None) -> None:
    """Reject malformed trusted shell recipes before opening execution effects."""
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        message = "trusted shell command must be a nonempty NUL-free string"
        raise ValueError(message)
    if timeout_seconds is not None and timeout_seconds <= 0:
        message = "command timeout must be positive"
        raise ValueError(message)


def validate_objectives(objectives: tuple[BenchmarkObjective, ...]) -> None:
    """Reject duplicate benchmark axes before trusted execution starts."""
    names = [objective.name for objective in objectives]
    if len(names) != len(set(names)):
        message = "benchmark objective names must be unique"
        raise ValueError(message)
