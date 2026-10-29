"""Policy-facing contracts for the reusable orchestration runtime."""

from __future__ import annotations

import hashlib
import inspect
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import (
    Path,
    PurePosixPath,
    PureWindowsPath,
)  # Pydantic resolves WorkspaceRef at runtime.
from typing import TYPE_CHECKING, Annotated, Any, Protocol, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from vs_core.api import AdoptionPublication, CheckoutUpdate, EvidenceRequirements, SpanKind
from vs_evaluation.api import EvidenceFailureKind
from vs_evaluator_protocol.api import PartialMeasurement
from vs_redaction.api import redact_diagnostic_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import (
        Limits,
        OperationRegistration,
        RequestRetries,
        RevisionRef,
        SearchDef,
    )
    from vs_core.api import RunFacts as CoreRunFacts
    from vs_github.api import GitHubClient
    from vs_project.api import OrchestrationDescriptor, Project
    from vs_runtime._agent_evaluation import AgentEvaluationPolicy
    from vs_runtime._artifact_store import ArtifactStore
    from vs_runtime._operation_catalog import OperationEntry
    from vs_runtime._operation_wiring import OperationRole
    from vs_runtime._run_environment import RunEnvironmentView
    from vs_runtime._trusted_evaluation import TrustedEvaluationPlan
    from vs_sim.api import Clock, OutputSink, Threads

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


class CaptureStopUnconfirmedError(RuntimeContractError):
    """A capture lifecycle cannot be proved safe for candidate cleanup.

    The command may still be running, or persistence of its durable intent or
    settlement may have failed. The runtime retains both the candidate and its
    temporary output when this is raised rather than guessing that cleanup is safe.
    """

    def __init__(self) -> None:
        """Describe the unresolved stop without backend-specific text."""
        super().__init__(
            "captured command settlement could not be confirmed; its candidate and output remain retained"
        )


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


class AgentWorkspaceRouteError(RuntimeContractError):
    """No sandbox serves the workspace an agent turn asked for.

    A turn sent to another workspace's container runs in a directory that is
    not there (exit 127, #1552), so a path that is neither the root nor a live
    candidate with a sandbox is refused rather than routed to a default.
    """

    def __init__(self, path: object, reason: str) -> None:
        """Name the workspace path and why no sandbox serves it."""
        super().__init__(f"no agent sandbox serves workspace {str(path)!r}: {reason}")


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
    """Closed agent capabilities a role may require."""

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
    stateless: bool = False
    """Whether the tool's server holds no run state (it only reads or analyses).

    A core run admits a tool only if it is the bridged evaluation tool or is stateless and
    served: any other tool may record into run state, which would be a second writer beside
    core. The default is the safe one.
    """


class Workspace(Protocol):
    """One live run workspace with semantic revision operations."""

    @property
    def id(self) -> str | None:
        """Return the immutable isolated ID (or root ``None``), even after release."""
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
        """Materialize a retained revision or raise :class:`WorkspaceRestoreError`.

        In a runtime-created candidate worktree the tree becomes exactly the revision,
        ignored files included. In the root workspace only untracked, non-ignored
        files are removed; ignored files (virtualenvs, caches) are never touched.
        """
        ...

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Try to materialize a revision, returning ``False`` on restore failure."""
        ...

    async def retain(self, revision: str, *, label: str) -> None:
        """Keep a revision reachable under a policy-owned semantic label."""
        ...

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        """Record the current tree and keep exactly that revision reachable."""
        ...

    async def has_revision(self, revision: str) -> bool:
        """Return whether this run's repository can materialize the revision."""
        ...

    async def matches_revision(self, revision: str) -> bool:
        """Return whether the materialized tree equals the revision's tree.

        Preserved framework memory is exempt. Ignored files count in a
        runtime-created candidate worktree and are not compared in the root workspace.
        """
        ...

    async def find_snapshot(self, label: str) -> str | None:
        """Return the revision a snapshot with this exact label created, if any."""
        ...

    async def measured_input(self, revision: str) -> str:
        """The digest of what the task's evaluation of *revision* reads.

        Two revisions that differ only in paths the task declares unread share it.
        """
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


class Checkout(Protocol):
    """A directory holding one revision's files, which its owner discards."""

    @property
    def path(self) -> Path:
        """Return the checkout's host path."""
        ...

    async def discard(self) -> None:
        """Release the checkout; safe to call more than once."""
        ...


class CheckoutRoot(Protocol):
    """The repository a revision is checked out from."""

    @property
    def path(self) -> Path:
        """Return the repository's host path."""
        ...

    async def has_revision(self, revision: str) -> bool:
        """Return whether the repository holds *revision*."""
        ...


class MeasuredCheckouts(Protocol):
    """What a trusted executor needs to measure a revision: a fresh checkout of it.

    ``Workspaces`` satisfies it; a measurement that belongs to no run (a comparison
    baseline's) supplies its own.
    """

    @property
    def root(self) -> CheckoutRoot:
        """Return the repository the revisions live in."""
        ...

    async def create_measured_candidate(self, revision: str) -> Checkout:
        """Return a fresh checkout of *revision*, holding what its evaluation reads."""
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

        A ``from_revision`` the run's repository does not hold (unknown, malformed, or not a
        commit) raises :class:`WorkspaceRestoreError`; nothing is created.
        """
        ...

    async def create_measured_candidate(self, revision: str) -> CandidateWorkspace:
        """Create an isolated candidate of *revision* to evaluate, as its measured input reads it.

        It holds *revision*'s files less the paths the task declares unread, so its files are
        exactly what ``Workspace.measured_input`` digests: an evaluation reads nothing the
        digest does not cover. Errors as :meth:`create_candidate`.
        """
        ...

    async def adopt(
        self, revision: str, *, message: str = "vibesys: adopt revision"
    ) -> AdoptionPublication:
        """Idempotently publish a retained revision on the run's publication branch.

        Never changes a checkout or its index. The returned commit stays reachable
        across resumes. Raises RuntimeContractError when publication is refused.
        """
        ...

    async def update_checkout(self, publication: AdoptionPublication) -> CheckoutUpdate:
        """Optionally fast-forward the run's own clean checked-out branch.

        Another branch, dirty tree, divergence or Git's overwrite refusal is a typed
        skip, and never invalidates the published adoption.
        """
        ...

    async def export_patch(self, revision: str) -> str:
        """Export a retained revision against the trusted-input baseline."""
        ...


class RevisionLedger(Protocol):
    """The run's record of which revisions it keeps reachable."""

    async def retains(self, revision: str) -> bool:
        """Whether this run keeps the revision reachable, not merely present.

        True for the root's history and for every revision a candidate retained or
        snapshotted. False for an unreferenced commit that still exists, and for an
        unknown revision. Exporting a patch proves only that an object exists.
        """
        ...


class CommandResult(BaseModel):
    """Bounded output from one sandboxed argv invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: str
    exit_code: int | None = None
    truncated: bool = False
    # Operator-facing form created before a command runner applies its output cap.
    diagnostic_output: str = ""

    @model_validator(mode="before")
    @classmethod
    def _require_prebounded_diagnostic(cls, value: object) -> object:
        """Derive from complete output, but never sanitize an already-truncated fragment."""
        if not isinstance(value, Mapping) or "diagnostic_output" in value:
            return value
        output = value.get("output")
        if isinstance(output, bytes):
            output = output.decode()
        if not isinstance(output, str):
            return value
        if value.get("truncated", False):
            if not output:
                return {**value, "diagnostic_output": ""}
            message = "a truncated command result requires diagnostic_output from before truncation"
            raise ValueError(message)
        return {**value, "diagnostic_output": redact_diagnostic_text(output)}


class CaptureCommands(Protocol):
    """Runtime-owned output capture scoped to a live run workspace."""

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
        the file after a successful command, and removes it after every settled
        path. A failed command returns its ordinary diagnostic output and exit
        status. An unconfirmed remote stop raises
        :class:`CaptureStopUnconfirmedError` and retains the candidate and
        artifact across host cleanup and restart. Failure to remove the artifact raises
        ``RuntimeContractError``
        unless another exception is already propagating, in which case the
        cleanup failure is attached as an exception note.
        """
        ...


class Commands(Protocol):
    """Ordinary sandboxed process execution scoped to a live run workspace."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Run one argv without policy-authored shell composition."""
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
    agent_git_writable: bool
    """Whether agents can commit where they write candidates; false when that Git metadata is read-only.

    Required: a run that forgot to resolve it must fail, not tell agents to commit.
    """
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


# Whether a benchmark failure describes its workload or execution infrastructure. It is
# the evidence's own failure kind, so the executor's claim reaches the evidence unconverted.
BenchmarkFailureKind = EvidenceFailureKind


class BuildEvaluation(BaseModel):
    """Semantic outcome of the candidate's confined build and the check of its outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    reused: bool = False
    """The commit's earlier verified build was reused instead of building again."""
    feedback: str | None = None
    operator_feedback: str | None = None
    """Credential-safe feedback for operator-facing projections, never agent evidence."""
    # Whose fault the failure is: present exactly when the build failed, with no default.
    failure_kind: BenchmarkFailureKind | None = None

    @model_validator(mode="after")
    def _failure_kind_exactly_when_failed(self) -> BuildEvaluation:
        if (self.feedback is None) != (self.failure_kind is None):
            message = "a build outcome carries a failure kind exactly when it failed"
            raise ValueError(message)
        if (self.feedback is None) != (self.operator_feedback is None):
            message = "a build failure requires a separate operator-safe feedback"
            raise ValueError(message)
        return self

    @property
    def passed(self) -> bool:
        """Return whether the build produced every required output."""
        return self.feedback is None


class AccuracyEvaluation(BaseModel):
    """Semantic outcome of the trusted accuracy evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    feedback: str | None = None
    operator_feedback: str | None = None
    """Credential-safe feedback for operator-facing projections, never agent evidence."""
    # Whose fault the failure is: present exactly when accuracy failed, with no default.
    failure_kind: BenchmarkFailureKind | None = None
    receipt: AccuracyReceipt | None = None

    @model_validator(mode="after")
    def _failure_kind_exactly_when_failed(self) -> AccuracyEvaluation:
        if (self.feedback is None) != (self.failure_kind is None):
            message = "an accuracy outcome carries a failure kind exactly when it failed"
            raise ValueError(message)
        if (self.feedback is None) != (self.operator_feedback is None):
            message = "an accuracy failure requires a separate operator-safe feedback"
            raise ValueError(message)
        return self

    @property
    def passed(self) -> bool:
        """Return whether policy may accept this accuracy outcome."""
        return self.feedback is None


class BenchmarkEvaluation(BaseModel):
    """Semantic outcome and measurements from the trusted benchmark."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    feedback: str | None = None
    operator_feedback: str | None = None
    """Credential-safe feedback for operator-facing projections, never agent evidence."""
    # Whose fault the failure is: present exactly when the benchmark failed, with no default.
    failure_kind: BenchmarkFailureKind | None = None
    metric_name: str | None = None
    metric_value: FiniteFloat | None = None
    metric_direction: MetricDirection | None = None
    metric_unit: str | None = None
    row: Mapping[str, FiniteFloat] | None = None
    # What a failed benchmark measured before it stopped, as its evaluator
    # reported it; absent when the evaluator reported nothing.
    partial_measurement: PartialMeasurement | None = None
    # The capability the evaluator named as unmet on a failed benchmark, when it did.
    unmet_requirement: str | None = None

    @model_validator(mode="after")
    def _failure_facts_only_when_failed(self) -> BenchmarkEvaluation:
        if self.partial_measurement is not None and self.feedback is None:
            message = "only a failed benchmark carries a partial measurement"
            raise ValueError(message)
        if (self.feedback is None) != (self.failure_kind is None):
            message = "a benchmark carries a failure kind exactly when it failed"
            raise ValueError(message)
        if self.unmet_requirement is not None and self.feedback is None:
            message = "only a failed benchmark names an unmet requirement"
            raise ValueError(message)
        if (self.feedback is None) != (self.operator_feedback is None):
            message = "a benchmark failure requires a separate operator-safe feedback"
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


class Evaluation(Protocol):
    """Trusted candidate evaluation effects a local run measures through."""

    async def build(self, workspace: Workspace, *, output: OutputSink) -> BuildEvaluation:
        """Build the workspace's commit confined, once, and verify its required outputs.

        A commit already built and verified reuses that build. The build command's raw output
        is written to *output* while it runs.
        """
        ...

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
        output: OutputSink,
    ) -> AccuracyEvaluation:
        """Evaluate correctness, optionally reusing an exact-candidate pass.

        The accuracy command's raw output is written to *output* while it runs.
        """
        ...

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
        output: OutputSink,
    ) -> BenchmarkEvaluation:
        """Measure one live workspace against policy-selected objectives.

        The benchmark command's raw output is written to *output* while it runs.
        """
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


@dataclass(frozen=True, slots=True)
class OrchestrationResumeDecision:
    """A plugin-approved descriptor update and its workspace precondition."""

    descriptor: OrchestrationDescriptor | None
    requires_clean_workspace: bool = False


@dataclass(frozen=True, slots=True)
class CoreRunContext:
    """What the host resolved before a core run starts, for the policy's per-run plan.

    Everything here is a fact about this run, never a choice: the validated options,
    the prompt-visible run facts, the trusted baseline revision, the trusted evaluation
    plan and the run environment it was resolved for, the run's artifact store, and the
    limits of the run environment and product config.
    """

    run_id: str
    options: BaseModel
    facts: RunFacts
    baseline: RevisionRef
    evaluation_plan: TrustedEvaluationPlan
    environment: RunEnvironmentView
    artifacts: ArtifactStore
    """The run's content-addressed artifact store, where a plan may store its recipe."""
    evaluation_capacity: int
    """How many evaluation jobs the run environment executes at once (at least 1)."""
    queue_allowance_seconds: int
    """How long a submitted evaluation may wait for capacity before it counts as stuck."""
    observe_interval_seconds: int
    """How often a running evaluation job is polled."""
    observe_backoff_cap_seconds: int
    """The longest wait after a poll that could not read the job."""
    queue_wait_budget_seconds: int
    """How long one measurement's job may wait in its executor's queue."""
    queue_notice_interval_seconds: int
    """How often a job still queued is reported, in seconds of queue time."""
    request_retries: RequestRetries
    """How the backend wants a request its executor did not run re-sent; a fact, not a choice."""
    max_run_seconds: int | None
    """Wall-clock budget of the run, or ``None`` for no deadline."""
    cancellation_bound_seconds: int
    """The longest core waits for a cancelled job to be confirmed gone."""
    cleanup_bound_seconds: int
    """The least time core gives its own cleanup requests once the deadline has passed."""
    reconciliation_bound_seconds: int
    """The longest core waits to learn what became of a request it could not read."""
    role_models: Mapping[str, str] = field(default_factory=dict)
    """The model the operator chose for a role (by role id) where it differs from the run's."""
    prompt_variables: Mapping[str, object] = field(default_factory=dict)
    """Data the host adds to the prompts of this run, by template variable name.

    Data only: the policy's templates own the wording, and a policy ignores a variable none of
    its templates reads.
    """


@dataclass(frozen=True, slots=True)
class CoreOperation:
    """One operation a strategy declares, and the runtime role that performs it."""

    role: OperationRole
    registration: OperationRegistration


@dataclass(frozen=True, slots=True)
class CorePlan:
    """What a policy resolves for one run: the pure search, its config and the facts it starts from.

    ``deadline_seconds`` is the run's budget, counted from the moment the run first
    starts; the host places it on the run clock's timeline.
    """

    search: SearchDef[Any]
    """The pure search; its declared roles say what each agent answers with."""
    config: object
    facts: CoreRunFacts
    limits: Limits
    deadline_seconds: float
    prompt_variables: Mapping[str, object] = field(default_factory=dict)
    requirements: EvidenceRequirements = field(default_factory=EvidenceRequirements)
    """Who may vouch for a candidate and which proofs make it winner-eligible."""
    agent_evaluation: AgentEvaluationPolicy | None = None
    """The plan an agent measures with through its in-turn tool; None offers no tool."""
    profile_capture_withheld: bool = False
    """The strategy would offer profile work but the run environment cannot capture a profile."""
    """Run-level template variables (such as the objective) every rendered prompt can read."""


@dataclass(frozen=True, slots=True)
class OwnedOperationResources:
    """What a policy builds the owners of its own operations from.

    ``variables`` are the plan's run-level template variables and ``templates`` is the
    directory of the policy's templates, the same two inputs the render operation reads.
    ``clock`` is the run's clock, for owners that stamp what they record, and ``threads`` makes
    the locks of owners that serialize their own state. ``github`` reaches GitHub, for owners
    that keep their records there.
    """

    run_id: str
    project: Project
    options: BaseModel
    variables: Mapping[str, object]
    templates: Path
    clock: Clock
    threads: Threads
    github: GitHubClient


@dataclass(frozen=True, slots=True)
class CorePolicy:
    """Declarative data that makes a plugin a core run: a pure plan and what it needs.

    The runtime owns the loop. The policy supplies the plan (resolved from the host's
    ``CoreRunContext`` once resources are open), the operations it registers and the
    runtime role behind each, and the labels and directories the operations use. No
    field is an async loop or a service; the host builds those from the run's resources.
    """

    plan: Callable[[CoreRunContext], CorePlan]
    operations: tuple[CoreOperation, ...]
    retention_label: str
    """Label of the snapshot that retains a verified revision."""
    prompt_templates: Path
    """Directory of the templates the strategy's render operation reads."""
    prompt_fallback_roots: tuple[Path, ...] = ()
    """Directories searched, in order, for a template or include the strategy's own lacks."""
    artifact_directories: tuple[str, ...] = ()
    """Workspace-relative directories a write-artifacts turn may write."""
    owned_operations: Callable[[OwnedOperationResources], tuple[OperationEntry, ...]] | None = None
    """Operations outside the runtime's roles, each with the owner the policy builds for the run.

    The runtime offers them beside the role operations, so a strategy that declares one as
    required fails at composition when the policy supplies no owner.
    """
    span_kinds: tuple[SpanKind, ...] = ()
    """The kinds of span the strategy labels (its ``SearchDef.spans``): the protocol exports
    each kind's attribute schema, so clients get its types before any run starts."""


@dataclass(frozen=True, slots=True)
class OrchestrationPlugin:
    """One validated orchestration and every policy value it owns.

    ``agents`` is the sole role catalog. Runtime implementations may construct
    a private lookup from it, but no second public registry can disagree.

    A plugin is declarative data for a run whose loop the runtime owns (see
    ``CorePolicy``): the runtime drives the plan, and the plugin has no loop of its own.
    """

    id: str
    agents: tuple[AgentRole, ...]
    options: type[BaseModel]
    core: CorePolicy
    config_version: int = 1

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
