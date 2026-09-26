"""Policy-facing contracts for the reusable orchestration runtime."""

from __future__ import annotations

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
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, overload

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, model_validator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ResponseT = TypeVar("ResponseT", bound=BaseModel)
_CONTROL_CHARACTER_LIMIT = 32


class RuntimeContractError(RuntimeError):
    """Base class for rejected runtime operations."""


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
    """An agent turn could not be parsed as its requested response type."""

    def __init__(self, role_id: str, response_type: type[BaseModel]) -> None:
        """Name the role and response contract whose validation failed."""
        super().__init__(
            f"agent role {role_id!r} did not return a valid {response_type.__name__} response"
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


class AgentTool(BaseModel):
    """Minimal immutable reference to one registered agent tool."""

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


class WorkspaceRef(BaseModel):
    """Immutable workspace value suitable for adapters and tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str | None = Field(default=None, min_length=1)
    path: Path


class AgentRole(BaseModel):
    """Complete immutable declaration of one policy-owned agent role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    system_prompt: str
    tools: tuple[AgentTool, ...] = ()
    skills: tuple[str, ...] = ()
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_WRITE
    required_capabilities: frozenset[AgentCapability] = frozenset()


class AgentBinding(BaseModel):
    """Immutable runtime choices resolved for one role session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str = Field(min_length=1)
    driver: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class AgentSession(Protocol):
    """One configured conversation with sequential, context-preserving turns."""

    @property
    def role(self) -> AgentRole:
        """Return the immutable role bound when this session was created."""
        ...

    @property
    def workspace(self) -> Workspace:
        """Return the immutable workspace handle bound at creation."""
        ...

    @property
    def member_id(self) -> str | None:
        """Return the durable policy identity, or ``None`` for a fresh session."""
        ...

    @property
    def writable_paths(self) -> tuple[str, ...]:
        """Return the fixed workspace-relative write grants for this session."""
        ...

    @property
    def binding(self) -> AgentBinding:
        """Return immutable harness and model attribution resolved by the runtime."""
        ...

    @property
    def closed(self) -> bool:
        """Return whether this session can accept more turns."""
        ...

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self, message: str, *, response: type[ResponseT] | None = None
    ) -> str | ResponseT:
        """Add one turn or raise :class:`AgentTurnTimeoutError` on timeout."""
        ...

    async def close(self) -> None:
        """Release session resources; safe to call more than once."""
        ...


class AgentSessions(Protocol):
    """Run-owned factory and lifetime owner for agent conversations."""

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        """Create a conversation with fixed write grants.

        ``member_id`` enables durable provider-session resume. ``writable_paths``
        is required only for ``LIMITED`` roles and is forbidden for the other
        access modes.
        """
        ...

    async def close(self) -> None:
        """Close every owned session in reverse creation order."""
        ...


class Workspaces(Protocol):
    """Run-owned access to live workspaces."""

    @property
    def root(self) -> Workspace:
        """Return the live root workspace for this run."""
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


class RunFacts(BaseModel):
    """Immutable prompt-visible facts resolved before orchestration starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain_id: str = Field(min_length=1)
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


class BenchmarkEvaluation(BaseModel):
    """Semantic outcome and measurements from the trusted benchmark."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    feedback: str | None = None
    metric_name: str | None = None
    metric_value: FiniteFloat | None = None
    metric_direction: MetricDirection | None = None
    metric_unit: str | None = None
    row: Mapping[str, FiniteFloat] | None = None

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

    @model_validator(mode="after")
    def _consistent_outcome(self) -> LocalValidationEvaluation:
        """Keep pass/fail state and policy-facing feedback unambiguous."""
        if self.passed == (self.feedback is not None):
            message = "passing local validation cannot have feedback; failure requires feedback"
            raise ValueError(message)
        return self


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


class RunStatus(StrEnum):
    """Terminal status returned by orchestration policy."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunHost(Protocol):
    """Small run-owned capability surface initially required by orchestration."""

    @property
    def run_id(self) -> str:
        """Return this run's stable identity."""
        ...

    @property
    def facts(self) -> RunFacts:
        """Return immutable prompt-visible facts fixed during run setup."""
        ...

    @property
    def agents(self) -> AgentSessions:
        """Return the run-owned agent-session capability."""
        ...

    @property
    def workspaces(self) -> Workspaces:
        """Return this run's live workspace capability."""
        ...

    @property
    def evaluation(self) -> Evaluation:
        """Return this run's trusted evaluation capability."""
        ...

    @property
    def state(self) -> State:
        """Return plugin-bound typed state durability."""
        ...

    @property
    def control(self) -> Control:
        """Return the cooperative operator-control capability."""
        ...

    @property
    def commands(self) -> Commands:
        """Return sandboxed argv execution for policy-selected commands."""
        ...

    @property
    def skills(self) -> Skills:
        """Return run-owned resolution for installed skill resources."""
        ...

    def log(self, message: str) -> None:
        """Record a presentation-neutral run log message."""
        ...


class ProjectedRound(BaseModel):
    """One completed policy round exposed to the VibeSys application."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    number: int
    status: Literal["completed", "failed"]
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"] | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    profile_skipped: bool = False


class PluginProjection(BaseModel):
    """Policy-owned JSON plus the generic facts observed by the application."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    payload: dict[str, JsonValue] | None
    rounds: tuple[ProjectedRound, ...] = ()
    experiment_revision: int | None = None


@dataclass(frozen=True, slots=True)
class OrchestrationPlugin:
    """One validated orchestration and every policy value it owns.

    ``agents`` is the sole role catalog. Runtime implementations may construct
    a private lookup from it, but no second public registry can disagree.
    """

    id: str
    agents: tuple[AgentRole, ...]
    options: type[BaseModel]
    orchestrate: Callable[[RunHost, BaseModel], Awaitable[RunStatus]]
    config_version: int = 1
    state: type[BaseModel] | None = None
    project: Callable[[BaseModel], PluginProjection] | None = None

    def __post_init__(self) -> None:
        """Reject duplicate role IDs before any run resources open."""
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", self.id) is None:
            message = f"invalid orchestration plugin ID {self.id!r}"
            raise ValueError(message)
        if self.config_version < 1:
            message = "orchestration plugin config version must be positive"
            raise ValueError(message)
        if self.options is BaseModel:
            message = "orchestration plugin options must be a concrete BaseModel subclass"
            raise ValueError(message)
        if self.project is not None and self.state is None:
            message = "orchestration plugin projection requires a declared state model"
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


def validate_member_id(member_id: str | None) -> None:
    """Require a nonempty logical identifier without control characters."""
    invalid = member_id is not None and (
        not isinstance(member_id, str)
        or not member_id.strip()
        or any(unicodedata.category(character) == "Cc" for character in member_id)
    )
    if invalid:
        message = f"invalid agent member ID {member_id!r}"
        raise ValueError(message)


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


def validate_objectives(objectives: tuple[BenchmarkObjective, ...]) -> None:
    """Reject duplicate benchmark axes before trusted execution starts."""
    names = [objective.name for objective in objectives]
    if len(names) != len(set(names)):
        message = "benchmark objective names must be unique"
        raise ValueError(message)
