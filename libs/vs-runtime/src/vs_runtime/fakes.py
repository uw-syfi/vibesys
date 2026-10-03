"""Faithful in-memory implementations of the public runtime contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, TypeAlias, TypeVar, overload

from pydantic import BaseModel

from vs_agent.api import NULL_SKILL_SELECTION
from vs_runtime._agent_declarations import (
    validate_agent_capabilities,
    validate_extra_tools,
)
from vs_runtime._trusted_evaluation import TrustedAccuracyResult, TrustedBenchmarkResult
from vs_runtime._workspace_access import unauthorized_paths
from vs_runtime.contracts import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentBinding,
    AgentCapability,
    AgentRole,
    AgentSession,
    BenchmarkEvaluation,
    BenchmarkObjective,
    CandidateWorkspace,
    CommandResult,
    LocalValidationEvaluation,
    OrchestrationPlugin,
    ResolvedSkillResources,
    Run,
    RunFacts,
    RuntimeContractError,
    SessionClosedError,
    SkillCatalogError,
    SkillResolution,
    SkillResourceRequest,
    StateModelError,
    UnknownAgentRoleError,
    Workspace,
    WorkspaceAccess,
    WorkspaceRestoreError,
    member_workspace_id,
    validate_command,
    validate_member_id,
    validate_objectives,
    validate_trusted_shell_command,
    validate_workspace_writable_paths,
)

if TYPE_CHECKING:
    from vs_runtime._agent_execution import AgentExecutionLifecycleEvent
    from vs_runtime._run_control import RunControlTransition
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox


class FakeAgentExecutionEnvironment:
    """In-memory scoped environment with observable idempotent cleanup."""

    def __init__(
        self,
        *,
        project_path_policy: ProjectPathPolicy,
        host_resources: tuple[HostResource, ...] = (),
        use_docker: bool = False,
        close_log: list[str] | None = None,
        name: str = "environment",
    ) -> None:
        """Configure environment facts and optional cleanup recording."""
        self.skill_source_dirs: tuple[Path, ...] = ()
        self.skill_selection = NULL_SKILL_SELECTION
        self.project_path_policy = project_path_policy
        self.host_resources = host_resources
        self.backends: dict[str, Sandbox] | None = None
        self.use_docker = use_docker
        self.closed = False
        self._close_log = close_log
        self._name = name

    def close(self) -> None:
        """Record cleanup exactly once."""
        if self.closed:
            return
        self.closed = True
        if self._close_log is not None:
            self._close_log.append(self._name)

    def agent_path(self, host_path: Path | str) -> str:
        """Return the identity mapping used by the in-memory environment."""
        return str(Path(host_path))


class FakeAgentExecutionLifecycleSink:
    """Record semantic execution events in emission order."""

    def __init__(self) -> None:
        """Create an empty lifecycle log."""
        self.events: list[AgentExecutionLifecycleEvent] = []

    def __call__(self, event: AgentExecutionLifecycleEvent) -> None:
        """Record one semantic lifecycle event."""
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class FakeTrustedAccuracyCall:
    """One trusted accuracy invocation observed by the Fake."""

    command_override: str | None


@dataclass(frozen=True, slots=True)
class FakeTrustedBenchmarkCall:
    """One trusted benchmark invocation observed by the Fake."""

    command_override: str | None
    required_metrics: frozenset[str]


class FakeTrustedEvaluationExecutor:
    """Scripted in-memory trusted evaluation mechanism."""

    def __init__(self) -> None:
        """Create an executor with passing, unexecuted defaults."""
        self.accuracy_calls: list[FakeTrustedAccuracyCall] = []
        self.benchmark_calls: list[FakeTrustedBenchmarkCall] = []
        self._accuracy_results: list[TrustedAccuracyResult] = []
        self._benchmark_results: list[TrustedBenchmarkResult] = []

    def script_accuracy(self, *results: TrustedAccuracyResult) -> None:
        """Replace queued accuracy outcomes."""
        self._accuracy_results = list(results)

    def script_benchmark(self, *results: TrustedBenchmarkResult) -> None:
        """Replace queued benchmark outcomes."""
        self._benchmark_results = list(results)

    async def accuracy(self, *, command_override: str | None = None) -> TrustedAccuracyResult:
        """Record one call and return the next scripted outcome."""
        self.accuracy_calls.append(FakeTrustedAccuracyCall(command_override))
        if self._accuracy_results:
            return self._accuracy_results.pop(0)
        return TrustedAccuracyResult(executed=False, passed=True)

    async def benchmark(
        self,
        *,
        command_override: str | None = None,
        required_metrics: frozenset[str] = frozenset(),
    ) -> TrustedBenchmarkResult:
        """Record one call and return the next scripted outcome."""
        self.benchmark_calls.append(FakeTrustedBenchmarkCall(command_override, required_metrics))
        if self._benchmark_results:
            return self._benchmark_results.pop(0)
        return TrustedBenchmarkResult(executed=False, passed=True)


ResponseT = TypeVar("ResponseT", bound=BaseModel)
TurnResponder: TypeAlias = Callable[
    [AgentRole, tuple[str, ...], str, type[BaseModel] | None], object
]


class FakeRunControlEventSink:
    """Record synchronous run-control transitions in emission order."""

    def __init__(
        self,
        *,
        on_transition: Callable[[RunControlTransition], None] | None = None,
    ) -> None:
        """Configure an optional deterministic reaction to each transition."""
        self.transitions: list[RunControlTransition] = []
        self._on_transition = on_transition

    def __call__(self, transition: RunControlTransition) -> None:
        """Record one transition and invoke the configured reaction."""
        self.transitions.append(transition)
        if self._on_transition is not None:
            self._on_transition(transition)


class FakeProjectMaterializationEffects:
    """Deterministic environment effects for project materialization tests."""

    def __init__(self, *, isolated: bool = False, removes_children: bool = True) -> None:
        """Configure symlink and privileged-removal behavior."""
        self._isolated = isolated
        self.removes_children = removes_children
        self.repaired: list[Path] = []
        self.removed: list[tuple[Path, str]] = []

    @property
    def isolated(self) -> bool:
        """Return the configured symlink policy."""
        return self._isolated

    def repair(self, workspace: Path) -> None:
        """Record a requested permission repair."""
        self.repaired.append(workspace)

    def remove_child(self, workspace: Path, name: str) -> bool:
        """Record and return the configured privileged-removal outcome."""
        self.removed.append((workspace, name))
        return self.removes_children


class FakeGitRunner:
    """In-memory command effect for pinned project-source checkouts."""

    def __init__(self, *, head: str, fail_command: str | None = None) -> None:
        """Configure the resolved commit and optional failing Git command."""
        self.head = head
        self.fail_command = fail_command
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> str:
        """Apply clone/checkout/rev-parse semantics without a subprocess."""
        command = args[0]
        self.calls.append((tuple(args), cwd))
        if command == self.fail_command:
            message = f"git {command} failed: configured fake failure"
            raise RuntimeError(message)
        if command == "clone":
            destination = Path(args[-1])
            (destination / ".git").mkdir(parents=True)
            return ""
        if command == "checkout":
            return ""
        if command == "rev-parse":
            return f"{self.head}\n"
        message = f"unsupported fake Git command: {command}"
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class FakeModelVolumeRequest:
    """One model-volume effect observed by a fake provisioner."""

    model_id: str
    revision: str | None


class FakeModelVolumeProvisioner:
    """Deterministic in-memory model-volume provisioner for composition tests."""

    def __init__(self) -> None:
        """Create an empty successful provisioner."""
        self._requests: list[FakeModelVolumeRequest] = []
        self._volumes: dict[FakeModelVolumeRequest, str] = {}
        self._failure: Exception | None = None

    @property
    def requests(self) -> tuple[FakeModelVolumeRequest, ...]:
        """Return provision attempts in call order."""
        return tuple(self._requests)

    def fail_with(self, error: Exception | None) -> None:
        """Configure an error for subsequent provision attempts."""
        self._failure = error

    def __call__(
        self,
        model_id: str,
        *,
        revision: str | None = None,
        log: Callable[[str], object] = print,
    ) -> str:
        """Record one request and return a stable volume name for its identity."""
        del log
        request = FakeModelVolumeRequest(model_id=model_id, revision=revision)
        self._requests.append(request)
        if self._failure is not None:
            raise self._failure
        if request not in self._volumes:
            self._volumes[request] = f"fake-model-volume-{len(self._volumes) + 1}"
        return self._volumes[request]


class _UnknownWorkspaceRevisionError(ValueError):
    def __init__(self, revision: str) -> None:
        super().__init__(f"workspace revision is not retained: {revision!r}")


class _WorkspaceRetentionLabelError(ValueError):
    def __init__(self) -> None:
        super().__init__("workspace retention label must be nonempty")


@dataclass(frozen=True)
class _FakeSessionConfig:
    """Immutable creation options shared by a fake session."""

    member_id: str | None
    writable_paths: tuple[str, ...]
    writable_directory_paths: tuple[str, ...]
    #: The resumed conversation's history, shared with earlier sessions.
    history: list[str] | None = None


@dataclass(frozen=True)
class _FakeCandidateConfig:
    """Creation state copied into one isolated fake workspace."""

    workspace_id: str
    path: Path
    revision: str
    trusted_input_baseline: str | None
    known_revisions: set[str]
    revision_prefix: str


def _echo_responder(
    _role: AgentRole,
    _history: tuple[str, ...],
    message: str,
    _response: type[BaseModel] | None,
) -> object:
    return message


class FakeAgentSession:
    """In-memory conversation with the public lifetime and context contract."""

    def __init__(
        self,
        role: AgentRole,
        workspace: FakeWorkspace,
        binding: AgentBinding,
        responder: TurnResponder,
        config: _FakeSessionConfig,
    ) -> None:
        """Bind a session to its configuration and, if resumed, its conversation."""
        self._role = role
        self._workspace = workspace
        self._member_id = config.member_id
        self._writable_paths = config.writable_paths
        self._writable_directory_paths = config.writable_directory_paths
        self._binding = binding
        self._responder = responder
        self._history: list[str] = [] if config.history is None else config.history
        self._turn_number = 0
        self._turn_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def role(self) -> AgentRole:
        """Return the role bound at creation."""
        return self._role

    @property
    def workspace(self) -> Workspace:
        """Return the workspace bound at creation."""
        return self._workspace

    @property
    def member_id(self) -> str | None:
        """Return the durable policy identity for this instance."""
        return self._member_id

    @property
    def writable_paths(self) -> tuple[str, ...]:
        """Return the immutable session-specific write grants."""
        return self._writable_paths

    @property
    def binding(self) -> AgentBinding:
        """Return the configured immutable runtime attribution."""
        return self._binding

    @property
    def closed(self) -> bool:
        """Return whether cleanup has ended this session."""
        return self._closed

    @property
    def history(self) -> tuple[str, ...]:
        """Return completed user messages in conversation order."""
        return tuple(self._history)

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self, message: str, *, response: type[ResponseT] | None = None
    ) -> str | ResponseT:
        """Serialize turns, respond from completed history, and enforce access."""
        if self._closed:
            raise SessionClosedError
        async with self._turn_lock:
            if self._closed:
                raise SessionClosedError
            return await self._turn_once(message, response=response)

    async def _turn_once(
        self,
        message: str,
        *,
        response: type[ResponseT] | None,
    ) -> str | ResponseT:
        self._turn_number += 1
        label = f"{self._role.id}-session-turn-{self._turn_number}"
        revision = await self._workspace.snapshot(f"{label}-input")
        try:
            value = self._responder(self._role, tuple(self._history), message, response)
            if response is None:
                if not isinstance(value, str):
                    error = "text turn responder must return str"
                    raise TypeError(error)
                result: str | ResponseT = value
            else:
                result = response.model_validate(value)
        finally:
            remaining_changes = await self._enforce_workspace_access(revision)
        self._history.append(message)
        if self._role.workspace_access is WorkspaceAccess.READ_WRITE or remaining_changes:
            await self._workspace.snapshot(label)
        return result

    async def _enforce_workspace_access(self, revision: str) -> list[str]:
        if self._role.workspace_access is WorkspaceAccess.READ_WRITE:
            return await self._workspace.pending_changes()
        allowed = (
            self._writable_paths if self._role.workspace_access is WorkspaceAccess.LIMITED else ()
        )
        directories = (
            self._writable_directory_paths
            if self._role.workspace_access is WorkspaceAccess.LIMITED
            else ()
        )
        changes = await self._workspace.pending_changes()
        unauthorized = unauthorized_paths(changes, allowed, directories=directories)
        if not unauthorized:
            return changes
        await self._workspace.restore_for_agent(revision, preserve_paths=allowed)
        remaining = await self._workspace.pending_changes()
        still_unauthorized = unauthorized_paths(
            remaining,
            allowed,
            directories=directories,
        )
        if still_unauthorized:
            detail = ", ".join(still_unauthorized)
            message = f"role {self._role.id!r} left unauthorized workspace changes: {detail}"
            raise RuntimeContractError(message)
        return remaining

    async def close(self) -> None:
        """Reject more work and wait for the active turn before closing."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        async with self._turn_lock:
            return

    def mark_closed(self) -> None:
        """Reject new and already-queued turns before owner cleanup starts."""
        self._closed = True


class FakeAgentSessions:
    """Run-owned in-memory factory with isolated creation semantics."""

    def __init__(
        self,
        agents: tuple[AgentRole, ...],
        *,
        responder: TurnResponder = _echo_responder,
        bindings: dict[str, AgentBinding] | None = None,
        supported_extra_tools: Collection[str] | None = None,
        supported_agent_capabilities: Collection[AgentCapability] | None = None,
    ) -> None:
        """Build role lookup, optionally restricting simulated driver support."""
        self._roles = {role.id: role for role in agents}
        self._responder = responder
        self._bindings = bindings or {
            role.id: AgentBinding(backend="fake", driver="fake", provider="fake") for role in agents
        }
        self._supported_extra_tools = frozenset(supported_extra_tools or ())
        default_capabilities = {AgentCapability.SESSION_REUSE}
        if self._supported_extra_tools:
            default_capabilities.add(AgentCapability.MCP_SERVERS)
        self._supported_agent_capabilities = frozenset(
            default_capabilities
            if supported_agent_capabilities is None
            else supported_agent_capabilities
        )
        # Keep observations separate from live ownership. Candidate teardown
        # removes sessions from the live set, but tests still need to inspect
        # what the public fake created and whether runtime ownership closed it.
        self._sessions: list[FakeAgentSession] = []
        self._active_sessions: list[FakeAgentSession] = []
        # Provider conversations by (role, member): the working directory the
        # conversation ran in and its shared history. Providers key resumable
        # history by working directory, so a member session continues only
        # from the same path.
        self._conversations: dict[tuple[str, str], tuple[Path, list[str]]] = {}
        self._creation_results: list[BaseException | None] = []
        self._closing = False
        self._closed = False

    @property
    def sessions(self) -> tuple[FakeAgentSession, ...]:
        """Return created sessions in ownership order."""
        return tuple(self._sessions)

    def script_creation(self, *results: BaseException | None) -> None:
        """Queue deterministic session-creation successes or failures."""
        self._creation_results.extend(results)

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        """Validate the declared role and create an independent conversation."""
        if self._closing:
            raise SessionClosedError
        if self._roles.get(role.id) != role:
            raise UnknownAgentRoleError(role.id)
        validate_member_id(member_id)
        bound_tool_ids = validate_extra_tools(role, self._supported_extra_tools)
        validate_agent_capabilities(
            role,
            self._supported_agent_capabilities,
            member_id=member_id,
            has_bound_tools=bool(bound_tool_ids),
        )
        validated_paths = validate_workspace_writable_paths(
            role.workspace_access,
            writable_paths,
        )
        if not isinstance(workspace, FakeWorkspace):
            message = "workspace is not owned by this fake runtime"
            raise TypeError(message)
        if self._creation_results:
            failure = self._creation_results.pop(0)
            if failure is not None:
                raise failure
        session = FakeAgentSession(
            role,
            workspace,
            self._bindings[role.id],
            self._responder,
            _FakeSessionConfig(
                member_id,
                validated_paths,
                tuple(path for path in validated_paths if workspace.is_directory(path)),
                self._member_history(role, member_id, workspace.path),
            ),
        )
        self._sessions.append(session)
        self._active_sessions.append(session)
        return session

    def _member_history(
        self, role: AgentRole, member_id: str | None, path: Path
    ) -> list[str] | None:
        """Return the conversation a member session resumes, or start one."""
        if member_id is None:
            return None
        key = (role.id, member_id)
        previous = self._conversations.get(key)
        if previous is not None and previous[0] == path:
            return previous[1]
        history: list[str] = []
        self._conversations[key] = (path, history)
        return history

    async def close(self) -> None:
        """Close all sessions in reverse creation order, once."""
        if self._closed:
            return
        self._closing = True
        self._closed = True
        for session in self._active_sessions:
            session.mark_closed()
        for session in reversed(self._active_sessions):
            await session.close()
        self._active_sessions.clear()

    def begin_close(self) -> None:
        """Reject new sessions before asynchronous teardown starts."""
        self._closing = True
        for session in self._active_sessions:
            session.mark_closed()

    async def close_workspace_sessions(self, workspace: Workspace) -> None:
        """Invalidate every public session bound to a discarded workspace."""
        sessions = [session for session in self._active_sessions if session.workspace is workspace]
        for session in sessions:
            session.mark_closed()
        for session in reversed(sessions):
            await session.close()
        selected = set(sessions)
        self._active_sessions = [
            session for session in self._active_sessions if session not in selected
        ]


class FakeWorkspaces:
    """In-memory owner of one root and its isolated candidate workspaces."""

    def __init__(
        self,
        root: FakeWorkspace,
        *,
        supports_parallel_candidates: bool = False,
        sessions: FakeAgentSessions | None = None,
    ) -> None:
        """Bind the fake capability to one root and a fixed isolation capability."""
        self._root = root
        self._supports_parallel_candidates = supports_parallel_candidates
        self._sessions = sessions
        self._candidates: list[FakeCandidateWorkspace] = []
        self._patches: dict[str, str] = {}
        self._default_patch: str | None = None
        self.export_patch_calls: list[str] = []
        self._closing = False
        self._closed = False

    @property
    def root(self) -> FakeWorkspace:
        """Return the configured fake root workspace."""
        return self._root

    @property
    def supports_parallel_candidates(self) -> bool:
        """Return the fixed candidate-isolation capability."""
        return self._supports_parallel_candidates

    @property
    def candidates(self) -> tuple[FakeCandidateWorkspace, ...]:
        """Return candidates in creation order, including discarded ones."""
        return tuple(self._candidates)

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
    ) -> CandidateWorkspace:
        """Create one isolated workspace from a known root revision."""
        if self._closing or self._closed:
            raise SessionClosedError
        if not self._supports_parallel_candidates:
            message = "this run does not support parallel candidate workspaces"
            raise RuntimeContractError(message)
        revision = from_revision or self._root.revision
        if revision is None or not self._root.knows_revision(revision):
            raise WorkspaceRestoreError(from_revision or "")
        if member_id is None:
            workspace_id = f"candidate-{len(self._candidates) + 1}"
        else:
            workspace_id = member_workspace_id(member_id)
            if any(
                candidate.id == workspace_id and not candidate.discarded
                for candidate in self._candidates
            ):
                message = f"member {member_id!r} already has a live candidate workspace"
                raise RuntimeContractError(message)
        candidate = FakeCandidateWorkspace(
            owner=self,
            invalidate_sessions=(
                self._sessions.close_workspace_sessions if self._sessions is not None else None
            ),
            config=_FakeCandidateConfig(
                workspace_id=workspace_id,
                path=self._root.path / workspace_id,
                revision=revision,
                trusted_input_baseline=self._root.trusted_input_baseline,
                known_revisions=self._root.known_revisions,
                revision_prefix=f"candidate-{len(self._candidates) + 1}",
            ),
        )
        self._candidates.append(candidate)
        return candidate

    async def adopt(self, revision: str) -> None:
        """Adopt a retained revision even after its candidate was discarded."""
        if not self._root.knows_revision(revision):
            message = f"candidate revision is not retained by this run: {revision!r}"
            raise RuntimeContractError(message)
        await self._root.restore(revision)

    async def export_patch(self, revision: str) -> str:
        """Export one retained revision against the fake trusted baseline."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self.export_patch_calls.append(revision)
        default = self._default_patch or f"patch for {revision}"
        return self._patches.get(revision, default)

    def set_patch(self, revision: str, patch: str) -> None:
        """Configure the canonical patch exported for a retained revision."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self._patches[revision] = patch

    def set_default_patch(self, patch: str) -> None:
        """Export ``patch`` for every revision without its own patch.

        Models snapshots that differ only in commits, not in content.
        """
        self._default_patch = patch

    def retain_candidate_revision(self, revision: str) -> None:
        """Keep a snapshotted candidate revision reachable from the root."""
        self._root.add_retained_revision(revision)

    def begin_close(self) -> None:
        """Reject new candidates and sessions before asynchronous teardown."""
        if self._closing:
            return
        self._closing = True
        if self._sessions is not None:
            self._sessions.begin_close()

    async def close(self) -> None:
        """Discard candidates, then close remaining root sessions, once."""
        if self._closed:
            return
        self.begin_close()
        self._closed = True
        for candidate in reversed(self._candidates):
            await candidate.discard()
        if self._sessions is not None:
            await self._sessions.close()


@dataclass(frozen=True, slots=True)
class FakeCommandCall:
    """One recorded sandboxed command request."""

    argv: tuple[str, ...]
    workspace: Workspace
    timeout_seconds: int | None
    output_argument: str | None = None


@dataclass(frozen=True, slots=True)
class FakeTrustedShellCall:
    """One recorded trusted shell recipe request."""

    command: str
    workspace: Workspace
    timeout_seconds: int | None


@dataclass(slots=True)
class FakeCommands:
    """Scriptable in-memory command execution with contract validation."""

    results: list[CommandResult | BaseException] = field(default_factory=list)
    calls: list[FakeCommandCall] = field(default_factory=list)
    trusted_shell_calls: list[FakeTrustedShellCall] = field(default_factory=list)

    def script(self, *results: CommandResult | BaseException) -> None:
        """Queue command results in invocation order."""
        self.results.extend(results)

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Validate and record one command, returning its scripted result."""
        validate_command(argv, timeout_seconds)
        self.calls.append(FakeCommandCall(argv, workspace, timeout_seconds))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Record one runtime-managed output capture and return its script."""
        validate_command(argv, timeout_seconds)
        validate_command((output_argument,), None)
        self.calls.append(FakeCommandCall(argv, workspace, timeout_seconds, output_argument))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)

    async def run_trusted_shell(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Validate and record one audited shell recipe."""
        validate_trusted_shell_command(command, timeout_seconds)
        self.trusted_shell_calls.append(FakeTrustedShellCall(command, workspace, timeout_seconds))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)


@dataclass(slots=True)
class FakeSkills:
    """In-memory skill catalog with the production resolver's observable rules."""

    installed_resources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    catalog_error: str | None = None

    async def resolve(self, requests: tuple[SkillResourceRequest, ...]) -> SkillResolution:
        """Resolve requests against installed names and their available files."""
        if not requests:
            return SkillResolution()
        if self.catalog_error is not None:
            raise SkillCatalogError(self.catalog_error)
        if not self.installed_resources:
            return SkillResolution(diagnostics=("no skill sources are installed",))

        return _resolve_fake_skills(requests, self.installed_resources)


def _resolve_fake_skills(
    requests: tuple[SkillResourceRequest, ...], installed: dict[str, tuple[str, ...]]
) -> SkillResolution:
    """Mirror production name merging and partial resource validation in memory."""
    merged: dict[str, tuple[str, list[str]]] = {}
    diagnostics: list[str] = []
    for index, request in enumerate(requests, start=1):
        name = request.name.strip()
        if name not in installed:
            diagnostics.append(f"selection #{index}: unknown installed skill {name!r}")
            continue
        if name not in merged:
            merged[name] = (request.purpose.strip(), [])
        purpose, resources = merged[name]
        diagnostics.extend(
            _resolve_fake_resources(
                request.resource_paths,
                name=name,
                selection_index=index,
                available=set(installed[name]),
                resources=resources,
            )
        )
        merged[name] = purpose, resources

    return SkillResolution(
        resolved=tuple(
            ResolvedSkillResources(
                name=name,
                router_path=f"{name}/SKILL.md",
                resource_paths=tuple(f"{name}/{path}" for path in resources),
                purpose=purpose,
            )
            for name, (purpose, resources) in merged.items()
        ),
        diagnostics=tuple(diagnostics),
    )


def _resolve_fake_resources(
    paths: tuple[str, ...],
    *,
    name: str,
    selection_index: int,
    available: set[str],
    resources: list[str],
) -> list[str]:
    """Validate one request's paths using the catalog's in-memory file set."""
    diagnostics: list[str] = []
    for raw_path in paths:
        resource = raw_path.strip()
        path = PurePosixPath(resource)
        if (
            not resource
            or "\\" in resource
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
        ):
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource path must be relative and stay within the skill"
            )
            continue
        if any(part in {".git", "repos", "__pycache__"} for part in path.parts):
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource path is excluded from agent skill materialization"
            )
            continue
        resource = path.as_posix()
        if resource not in available:
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource file does not exist"
            )
            continue
        if resource != "SKILL.md" and resource not in resources:
            resources.append(resource)
    return diagnostics


class FakeWorkspace:
    """In-memory live workspace with revision and retention semantics."""

    def __init__(
        self,
        *,
        path: Path = Path(),
        workspace_id: str | None = None,
        revision: str | None = "fake-revision",
        trusted_input_baseline: str | None = None,
        known_revisions: set[str] | None = None,
    ) -> None:
        """Create a workspace at one recorded tree and immutable baseline."""
        self._path = path
        self._id = workspace_id
        self._revision = revision
        self._tree_revision = revision
        self._trusted_input_baseline = (
            revision if trusted_input_baseline is None else trusted_input_baseline
        )
        self._known_revisions = set(known_revisions or ()) | {
            value for value in (revision, self._trusted_input_baseline) if value
        }
        self._snapshot_count = 0
        # Revision names stay unique when a member-keyed candidate reuses an ID.
        self._revision_prefix = workspace_id or "fake"
        self._retained: dict[str, str] = {}
        self._pending_changes: list[list[str]] = []
        self._directories: set[str] = set()
        self.restore_calls: list[tuple[str, bool]] = []
        self.agent_restore_calls: list[tuple[str, tuple[str, ...]]] = []

    @property
    def id(self) -> str | None:
        """Return the configured workspace identity."""
        return self._id

    @property
    def path(self) -> Path:
        """Return the configured host path without touching the filesystem."""
        return self._path

    @property
    def revision(self) -> str | None:
        """Return the latest recorded fake revision."""
        return self._revision

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the immutable configured trusted-input baseline."""
        return self._trusted_input_baseline

    @property
    def retained(self) -> dict[str, str]:
        """Return policy labels and revisions retained so far."""
        return dict(self._retained)

    async def snapshot(self, label: str) -> str:
        """Record a deterministic new revision for the current fake tree."""
        del label
        self._snapshot_count += 1
        revision = f"{self._revision_prefix}-revision-{self._snapshot_count}"
        self._revision = revision
        self._tree_revision = revision
        self._known_revisions.add(revision)
        return revision

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Materialize a known tree while leaving recorded history unchanged."""
        self.restore_calls.append((revision, clean))
        if revision not in self._known_revisions:
            raise WorkspaceRestoreError(revision)
        self._tree_revision = revision

    def script_pending_changes(self, *changes: list[str]) -> None:
        """Queue workspace mutation observations in invocation order."""
        self._pending_changes.extend(changes)

    async def pending_changes(self) -> list[str]:
        """Return the next scripted uncommitted-change listing."""
        if self._pending_changes:
            return self._pending_changes.pop(0)
        return []

    async def restore_for_agent(
        self,
        revision: str,
        *,
        preserve_paths: tuple[str, ...],
    ) -> None:
        """Record an isolation restore while preserving only explicit grants."""
        if revision not in self._known_revisions:
            raise WorkspaceRestoreError(revision)
        self._tree_revision = revision
        self.agent_restore_calls.append((revision, preserve_paths))

    def declare_directories(self, *paths: str) -> None:
        """Declare which validated writable paths represent directories."""
        self._directories.update(paths)

    def is_directory(self, path: str) -> bool:
        """Return whether a writable grant covers descendants of *path*."""
        return path in self._directories

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Return whether a known tree could be materialized."""
        try:
            await self.restore(revision, clean=clean)
        except WorkspaceRestoreError:
            return False
        return True

    async def retain(self, revision: str, *, label: str) -> None:
        """Retain a known revision under a nonempty semantic label."""
        if revision not in self._known_revisions:
            raise _UnknownWorkspaceRevisionError(revision)
        if not label:
            raise _WorkspaceRetentionLabelError
        self._retained[label] = revision

    def knows_revision(self, revision: str) -> bool:
        """Return whether this fake can materialize a revision."""
        return revision in self._known_revisions

    @property
    def known_revisions(self) -> set[str]:
        """Return a copy of the revisions reachable from this fake workspace."""
        return set(self._known_revisions)

    def add_retained_revision(self, revision: str) -> None:
        """Make an externally retained revision materializable."""
        self._known_revisions.add(revision)


class FakeCandidateWorkspace(FakeWorkspace):
    """Faithful isolated fake with explicit, idempotent lifetime."""

    def __init__(
        self,
        *,
        owner: FakeWorkspaces,
        invalidate_sessions: Callable[[Workspace], Awaitable[None]] | None,
        config: _FakeCandidateConfig,
    ) -> None:
        """Bind a candidate to the one fake run that created it."""
        super().__init__(
            workspace_id=config.workspace_id,
            path=config.path,
            revision=config.revision,
            trusted_input_baseline=config.trusted_input_baseline,
            known_revisions=config.known_revisions,
        )
        self._owner = owner
        self._invalidate_sessions = invalidate_sessions
        self._discarded = False
        self._revision_prefix = config.revision_prefix

    @property
    def discarded(self) -> bool:
        """Return whether the isolated workspace has been released."""
        return self._discarded

    @property
    def path(self) -> Path:
        """Return the isolated path while its resources are live."""
        self._require_open()
        return super().path

    @property
    def revision(self) -> str | None:
        """Return the recorded candidate revision while resources are live."""
        self._require_open()
        return super().revision

    async def snapshot(self, label: str) -> str:
        """Record a candidate revision while the workspace is live."""
        self._require_open()
        revision = await super().snapshot(label)
        self._owner.retain_candidate_revision(revision)
        return revision

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Restore a candidate revision while the workspace is live."""
        self._require_open()
        await super().restore(revision, clean=clean)

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Try to restore a candidate revision while the workspace is live."""
        self._require_open()
        return await super().try_restore(revision, clean=clean)

    async def retain(self, revision: str, *, label: str) -> None:
        """Retain a candidate revision while the workspace is live."""
        self._require_open()
        await super().retain(revision, label=label)

    async def discard(self) -> None:
        """Release this fake candidate idempotently."""
        if self._discarded:
            return
        if self._invalidate_sessions is not None:
            await self._invalidate_sessions(self)
        self._discarded = True

    def _require_open(self) -> None:
        """Reject operations whose isolated resources no longer exist."""
        if self._discarded:
            message = "candidate workspace is closed"
            raise RuntimeContractError(message)


class FakeControl:
    """Deterministic cooperative-control boundary for plugin tests."""

    def __init__(self) -> None:
        """Create an active control with no pending failure."""
        self._failure: BaseException | None = None
        self._checkpoints = 0

    @property
    def checkpoints(self) -> int:
        """Return how many checkpoints plugin control flow reached."""
        return self._checkpoints

    def fail_with(self, error: BaseException | None) -> None:
        """Configure the error raised at subsequent checkpoints, or clear it."""
        self._failure = error

    async def checkpoint(self) -> None:
        """Record the boundary and raise its configured stop failure, if any."""
        self._checkpoints += 1
        if self._failure is not None:
            raise self._failure


@dataclass(frozen=True, slots=True)
class FakeStateCommit:
    """One validated state replacement recorded by :class:`FakeState`."""

    value: BaseModel
    workspace: Workspace | None
    label: str | None


class FakeState:
    """Faithful in-memory plugin-state durability with detached values."""

    def __init__(self, model: type[BaseModel] | None, root: Workspace) -> None:
        """Bind the exact plugin declaration and its only live workspace."""
        self._model = model
        self._root = root
        self._value: BaseModel | None = None
        self._commits: list[FakeStateCommit] = []
        self._commit_results: list[BaseException | None] = []

    @property
    def commits(self) -> tuple[FakeStateCommit, ...]:
        """Return detached commit records in durability order."""
        return tuple(
            FakeStateCommit(
                type(commit.value).model_validate_json(
                    commit.value.model_dump_json(round_trip=True)
                ),
                commit.workspace,
                commit.label,
            )
            for commit in self._commits
        )

    def script_commit(self, *results: BaseException | None) -> None:
        """Queue deterministic durable-commit successes or failures."""
        self._commit_results.extend(results)

    async def load(self, model: type[ResponseT]) -> ResponseT | None:
        """Return a detached value after validating the exact declared model."""
        self._require_model(model)
        if self._value is None:
            return None
        return model.model_validate_json(self._value.model_dump_json(round_trip=True))

    async def commit(
        self,
        value: BaseModel,
        *,
        workspace: Workspace | None = None,
        label: str | None = None,
    ) -> None:
        """Record one deep-validated replacement and optional root association."""
        self._require_model(type(value))
        if workspace is not None and workspace is not self._root:
            message = "state can commit only the live root workspace for this run"
            raise RuntimeContractError(message)
        model = self._model
        if model is None:
            raise StateModelError(None, type(value))
        snapshot = model.model_validate_json(value.model_dump_json(round_trip=True))
        if self._commit_results:
            failure = self._commit_results.pop(0)
            if failure is not None:
                raise failure
        self._value = snapshot
        recorded = model.model_validate_json(snapshot.model_dump_json(round_trip=True))
        self._commits.append(FakeStateCommit(recorded, workspace, label))

    def _require_model(self, model: type[BaseModel]) -> None:
        if model is not self._model:
            raise StateModelError(self._model, model)


@dataclass(frozen=True, slots=True)
class FakeAccuracyCall:
    """One recorded accuracy evaluation request."""

    workspace: Workspace
    reuse: AccuracyReceipt | None


@dataclass(frozen=True, slots=True)
class FakeBenchmarkCall:
    """One recorded benchmark evaluation request."""

    workspace: Workspace
    objectives: tuple[BenchmarkObjective, ...]


@dataclass(frozen=True, slots=True)
class FakeLocalValidationCall:
    """One recorded candidate-authored local validation request."""

    workspace: Workspace
    recipe_artifact: str
    report_location: str


@dataclass(slots=True)
class FakeEvaluation:
    """Scriptable in-memory implementation of trusted evaluation effects."""

    default_accuracy: AccuracyEvaluation = field(
        default_factory=lambda: AccuracyEvaluation(executed=False)
    )
    default_benchmark: BenchmarkEvaluation = field(
        default_factory=lambda: BenchmarkEvaluation(executed=False)
    )
    default_local_validation: LocalValidationEvaluation = field(
        default_factory=lambda: LocalValidationEvaluation(passed=True)
    )
    accuracy_results: list[AccuracyEvaluation] = field(default_factory=list)
    benchmark_results: list[BenchmarkEvaluation] = field(default_factory=list)
    local_validation_results: list[LocalValidationEvaluation] = field(default_factory=list)
    accuracy_calls: list[FakeAccuracyCall] = field(default_factory=list)
    benchmark_calls: list[FakeBenchmarkCall] = field(default_factory=list)
    local_validation_calls: list[FakeLocalValidationCall] = field(default_factory=list)
    run_id: str = "test-run"

    def script_accuracy(self, *results: AccuracyEvaluation) -> None:
        """Queue accuracy results in call order."""
        self.accuracy_results.extend(results)

    def script_benchmark(self, *results: BenchmarkEvaluation) -> None:
        """Queue benchmark results in call order."""
        self.benchmark_results.extend(results)

    def script_local_validation(self, *results: LocalValidationEvaluation) -> None:
        """Queue local-validation results in call order."""
        self.local_validation_results.extend(results)

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        """Record the request and return the next scripted result."""
        self.accuracy_calls.append(FakeAccuracyCall(workspace, reuse))
        if reuse is not None:
            if reuse.run_id != self.run_id:
                message = "accuracy receipt belongs to another run"
                raise RuntimeContractError(message)
            if reuse.workspace_id != workspace.id:
                message = "accuracy receipt belongs to another workspace"
                raise RuntimeContractError(message)
            if reuse.revision != workspace.revision:
                message = "accuracy receipt does not match the current workspace revision"
                raise RuntimeContractError(message)
            return AccuracyEvaluation(executed=False, receipt=reuse)
        result = self.accuracy_results.pop(0) if self.accuracy_results else self.default_accuracy
        if not result.passed:
            return result
        revision = workspace.revision
        if revision is None:
            message = "accuracy requires a recorded workspace revision"
            raise RuntimeContractError(message)
        receipt = result.receipt or AccuracyReceipt(
            run_id=self.run_id,
            workspace_id=workspace.id,
            revision=revision,
        )
        return result.model_copy(update={"receipt": receipt})

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
    ) -> BenchmarkEvaluation:
        """Record the request and return the next scripted result."""
        validate_objectives(objectives)
        self.benchmark_calls.append(FakeBenchmarkCall(workspace, objectives))
        if self.benchmark_results:
            return self.benchmark_results.pop(0)
        return self.default_benchmark

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Record one semantic local-validation request and return its script."""
        validate_workspace_writable_paths(
            WorkspaceAccess.LIMITED,
            (recipe_artifact, report_location),
        )
        self.local_validation_calls.append(
            FakeLocalValidationCall(workspace, recipe_artifact, report_location)
        )
        if self.local_validation_results:
            return self.local_validation_results.pop(0)
        return self.default_local_validation


@dataclass(frozen=True, slots=True)
class ObservationCall:
    """One ordered call captured by :class:`FakeObservations`."""

    kind: Literal["note", "warning"]
    message: str


class FakeObservations:
    """In-memory observation capability preserving kind and call order."""

    def __init__(self) -> None:
        """Create an empty ordered observation log."""
        self._calls: list[ObservationCall] = []

    @property
    def calls(self) -> tuple[ObservationCall, ...]:
        """Return observation calls in publication order."""
        return tuple(self._calls)

    def note(self, message: str) -> None:
        """Capture one informational note."""
        self._calls.append(ObservationCall("note", message))

    def warning(self, message: str) -> None:
        """Capture one non-fatal warning."""
        self._calls.append(ObservationCall("warning", message))


class FakeRun(Run):
    """In-memory run value with scriptable capabilities and owned cleanup."""

    agents: FakeAgentSessions
    workspaces: FakeWorkspaces
    evaluation: FakeEvaluation
    state: FakeState
    control: FakeControl
    commands: FakeCommands
    skills: FakeSkills
    observations: FakeObservations
    _closed: bool

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-040117 [PLR0913]; these are independent fake inputs, and a fake-host options bundle would add a second public configuration shape solely to shorten this signature.
        self,
        plugin: OrchestrationPlugin,
        *,
        run_id: str = "test-run",
        project_root: Path = Path(),
        facts: RunFacts | None = None,
        responder: TurnResponder = _echo_responder,
        agent_bindings: dict[str, AgentBinding] | None = None,
        supported_extra_tools: Collection[str] | None = None,
        supported_agent_capabilities: Collection[AgentCapability] | None = None,
        supports_parallel_candidates: bool = False,
    ) -> None:
        """Create a run whose private role map derives from ``plugin.agents``."""
        facts = (
            RunFacts(domain_id="generic", objective="Test objective.") if facts is None else facts
        )
        agents = FakeAgentSessions(
            plugin.agents,
            responder=responder,
            bindings=agent_bindings,
            supported_extra_tools=supported_extra_tools,
            supported_agent_capabilities=supported_agent_capabilities,
        )
        workspaces = FakeWorkspaces(
            FakeWorkspace(path=project_root),
            supports_parallel_candidates=supports_parallel_candidates,
            sessions=agents,
        )
        super().__init__(
            run_id=run_id,
            facts=facts,
            agents=agents,
            workspaces=workspaces,
            evaluation=FakeEvaluation(run_id=run_id),
            state=FakeState(plugin.state, workspaces.root),
            control=FakeControl(),
            commands=FakeCommands(),
            skills=FakeSkills(),
            observations=FakeObservations(),
        )
        object.__setattr__(self, "_closed", False)

    async def close(self) -> None:
        """Close all run-owned resources idempotently."""
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        self.workspaces.begin_close()
        await self.workspaces.close()
